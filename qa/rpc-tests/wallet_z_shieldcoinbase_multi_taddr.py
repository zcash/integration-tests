#!/usr/bin/env python3
# Copyright (c) 2025-2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Multi-receiver coverage for the `z_shieldcoinbase` UUID-form sweep.
#
# `wallet_z_shieldcoinbase.py` exercises the UUID form when the account
# has coinbase on a single transparent receiver (the auto-provisioned
# miner address, at `KeyScope::INTERNAL`). The UUID resolver inside
# Zallet expands to every transparent receiver of the account — both
# `INTERNAL` and `EXTERNAL`.
#
# Zebrad does not provide a mechanism to generate blocks with a specified
# coinbase address, and instead only generates blocks with the miner address
# specified in the zebrad config. So, producing coinbase on two transparent
# receivers in the same account requires switching zebrad's
# `mining.miner_address` mid-test. A naive stop+restart leaves the wallet's
# SQLite block records pointing at the pre-restart non-finalized tail (lost
# when zebrad drops its in-memory state on shutdown, per
# `MAX_BLOCK_REORG_HEIGHT = 99`). Re-mining the same heights with a different
# miner address produces different block hashes, and the wallet then upserts
# those heights with conflicting data and errors out with
# `SqliteClientError::BlockConflict`.
#
# To avoid that, the test:
#   1. Mines enough blocks at miner_address_A to push block 1 (the
#      one zallet-startup block) past the finalization horizon — so
#      A's coinbase output at block 1 survives zebrad restart.
#   2. Stops zallet and zebrad.
#   3. Truncates the wallet's block-scan state via
#      `zallet repair truncate-wallet` to discard the wallet's stale
#      view of the non-finalized tail that zebrad will drop.
#   4. Restarts zebrad with `mining.miner_address = B`, then restarts
#      zallet. The wallet re-scans the chain from a fresh slate; no
#      records conflict.
#   5. Mines coinbase to B until both A's and B's coinbase outputs
#      are mature.
#   6. Sweeps via the UUID form and asserts that the sweep covered
#      coinbase across BOTH receivers.
#

import os
import subprocess
import time

from decimal import Decimal
from test_framework.test_framework import ZcashTestFramework
from test_framework.config import ZebraArgs
from test_framework.util import (
    assert_equal,
    assert_true,
    start_node,
    start_nodes,
    start_wallets,
    stop_node,
    stop_wallets,
    wait_and_assert_operationid_status,
    wait_nodes,
    wait_zallets,
    wallet_dir,
    zallet_binary,
)

# Coinbase outputs require 100 confirmations before they can be spent.
COINBASE_MATURITY = 100

# zebra-state's MAX_BLOCK_REORG_HEIGHT. Blocks at depth >= this value
# are committed to the finalized state and persist across zebrad
# restart; shallower blocks live in memory and are lost on shutdown.
ZEBRA_MAX_REORG_HEIGHT = 99


def first_transparent_receiver(wallet, ua):
    receivers = wallet.z_listunifiedreceivers(ua)
    if 'p2pkh' in receivers:
        return receivers['p2pkh']
    if 'p2sh' in receivers:
        return receivers['p2sh']
    raise AssertionError(
        "UA has no transparent receiver: {!r} -> {!r}".format(ua, receivers))


class WalletZShieldCoinbaseMultiTaddrTest(ZcashTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights={"NU5": 1},
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def _mature_coinbase_on(self, wallet, taddr):
        utxos = wallet.z_listunspent(COINBASE_MATURITY + 1)
        return [u for u in utxos
                if u.get('pool') == 'transparent' and u.get('address') == taddr]

    def run_test(self):
        node = self.nodes[0]
        w0 = self.wallets[0]
        # `prepare_wallets_for_mining` captures the raw zallet CLI
        # stdout; normalize for string comparison against
        # `z_listunspent`'s canonical encoding.
        taddr_a = self.miner_addresses[0].strip()

        accounts = w0.z_listaccounts()
        assert_true(len(accounts) >= 1, "Wallet 0 should have at least one account")
        account_uuid = accounts[0]['account_uuid']

        # Destination for the sweep. Sapling+Orchard receivers let the
        # change strategy pick whichever pool it prefers.
        zaddr = w0.z_getaddressforaccount(
            account_uuid, ["sapling", "orchard"])['address']

        # Resolve a second transparent receiver (EXTERNAL scope) on
        # the same account. Pre-restart resolution is important: the
        # post-restart wallet may not have this UA in its registry
        # depending on how `z_getaddressforaccount` interacts with
        # truncate, but zebrad only needs the string and the address
        # is derivable from the account's seed regardless.
        ua_with_taddr = w0.z_getaddressforaccount(
            account_uuid, ["orchard", "p2pkh"])['address']
        taddr_b = first_transparent_receiver(w0, ua_with_taddr)
        assert_true(taddr_b != taddr_a,
                    "Second receiver must differ from miner_address")
        print("  Account 0 UUID:    {}".format(account_uuid))
        print("  miner_address A:   {}".format(taddr_a))
        print("  second receiver B: {}".format(taddr_b))

        # ---- Phase 1: mine to A, finalizing exactly block 1. -------
        # `prepare_chain` already mined block 1 (coinbase to A). Mine
        # ZEBRA_MAX_REORG_HEIGHT (99) more to push block 1 to depth 99
        # — the finalization horizon — and leave blocks 2..100 at
        # depth 0..97, all non-finalized. zebrad will drop the
        # non-finalized tail on restart, leaving exactly one A
        # coinbase output (block 1) preserved across the miner switch.
        print("Mining {} blocks at miner=A to finalize block 1...".format(
            ZEBRA_MAX_REORG_HEIGHT))
        node.generate(ZEBRA_MAX_REORG_HEIGHT)
        # Chain tip = 1 + 99 = 100. Block 1 depth 99 → finalized.

        # ---- Phase 2: stop, truncate, switch miner to B. -----------
        print("Stopping wallet and node before truncate...")
        stop_wallets(self.wallets)
        wait_zallets()
        stop_node(self.nodes[0], 0)
        wait_nodes()

        # Truncate the wallet's block-scan state so it no longer holds
        # records for heights zebrad is about to lose. After shutdown
        # zebrad only persists blocks at depth >= ZEBRA_MAX_REORG_HEIGHT;
        # the rest live in memory and are discarded. Re-mining those
        # heights with a different miner address produces different
        # block hashes; without truncate, the wallet upserts mismatched
        # hashes -> `BlockConflict`.
        #
        # Truncate to 1: matches what zebrad still has post-restart
        # (block 1), discards records for the about-to-be-lost tail.
        # 1 is the lowest usable target — there is no `blocks` row at
        # height 0.
        datadir = wallet_dir(self.options.tmpdir, 0)
        print("Truncating wallet block-scan state to height 1...")
        result = subprocess.run(
            [zallet_binary(), "-d=" + datadir,
             "repair", "truncate-wallet", "1"],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(
                "zallet repair truncate-wallet failed (rc={}):\n"
                "stdout:\n{}\nstderr:\n{}".format(
                    result.returncode, result.stdout, result.stderr))

        # Workaround for zcash/wallet#447: the post-restart wallet
        # would otherwise query the un-mined transactions truncate
        # left behind, get a "no such tx" response, and exit. Pre-
        # retire those rows here so the query skips them. Remove once
        # zcash/wallet#447 is resolved.
        wallet_db = os.path.join(datadir, "wallet.db")
        subprocess.run(
            ["sqlite3", wallet_db,
             "UPDATE transactions SET confirmed_unmined_at_height = 2000000000 "
             "WHERE mined_height IS NULL;"],
            check=True, capture_output=True, text=True)

        print("Restarting zebrad with miner=B...")
        self.nodes[0] = start_node(
            0, self.options.tmpdir,
            ZebraArgs(miner_address=taddr_b,
                      activation_heights={"NU5": 1}))
        node = self.nodes[0]
        self.wallets = start_wallets(self.num_wallets, self.options.tmpdir)
        w0 = self.wallets[0]

        # ---- Phase 3: mine to B until exactly one B coinbase matures.
        # Post-restart zebrad has just block 1. Mine COINBASE_MATURITY+1
        # (101) more so:
        #   tip = 102
        #   block 1 (A) confirmations = 102 → mature
        #   block 2 (B) confirmations = 101 → mature
        #   block 3 (B) confirmations = 100 → NOT mature
        # i.e. exactly one mature coinbase on each receiver.
        print("Mining {} blocks at miner=B...".format(COINBASE_MATURITY + 1))
        node.generate(COINBASE_MATURITY + 1)

        # Wait for the wallet to scan to chain tip — readiness signal
        # is the expected 1+1 mature coinbase state.
        print("Waiting for wallet to see exactly 1 mature coinbase on each receiver...")
        deadline = time.time() + 240
        mature_a = []
        mature_b = []
        while time.time() < deadline:
            mature_a = self._mature_coinbase_on(w0, taddr_a)
            mature_b = self._mature_coinbase_on(w0, taddr_b)
            if len(mature_a) == 1 and len(mature_b) == 1:
                break
            time.sleep(1)

        # ---- Phase 4: pre-sweep assertions (exact). ----------------
        # Expect 1 mature coinbase on each receiver: block 1 (A) at
        # 102 confirmations, block 2 (B) at 101 confirmations.
        assert_equal(
            len(mature_a), 1,
            "Pre-sweep: expected exactly 1 mature coinbase on A (block 1), saw {}".format(
                len(mature_a)))
        assert_equal(
            len(mature_b), 1,
            "Pre-sweep: expected exactly 1 mature coinbase on B (block 2), saw {}".format(
                len(mature_b)))
        coinbase_value_a = Decimal(mature_a[0]['value'])
        coinbase_value_b = Decimal(mature_b[0]['value'])
        pre_transparent_total = coinbase_value_a + coinbase_value_b

        # No shielded receives have happened yet. (z_gettotalbalance's
        # `transparent` field sums every mined transparent UTXO the
        # wallet holds, including immature coinbase — not what we want
        # for "mature-only" — so we don't assert on it here; the per-
        # receiver z_listunspent counts above already pin the mature
        # set exactly.)
        balance = w0.z_gettotalbalance(COINBASE_MATURITY + 1, True)
        assert_equal(
            Decimal(balance['private']), Decimal('0'),
            "Pre-sweep: expected 0 shielded balance, saw {}".format(balance['private']))
        print("  Pre-sweep: 1 mature on A ({} ZEC), 1 mature on B ({} ZEC), 0 shielded".format(
            coinbase_value_a, coinbase_value_b))

        # ---- Phase 5: UUID-form sweep. -----------------------------
        print("Sweeping via UUID form...")
        result = w0.z_shieldcoinbase(account_uuid, zaddr, None, None, None, None)

        # Pre-flight shape sanity.
        for key in ('remainingUTXOs', 'remainingValue',
                    'shieldingUTXOs', 'shieldingValue', 'opid'):
            assert_true(key in result, "Missing field {!r} in response".format(key))

        # Sweep must select exactly the two mature coinbases — one
        # from each receiver. Anything other than 2 means either the
        # UUID resolver silently skipped a receiver, or the privacy
        # policy refused to link same-account addresses; both are
        # regressions this test guards against.
        assert_equal(
            result['shieldingUTXOs'], 2,
            "UUID sweep should select exactly 2 UTXOs (1 from each receiver); "
            "got {}".format(result['shieldingUTXOs']))
        assert_equal(
            Decimal(result['shieldingValue']), pre_transparent_total,
            "shieldingValue should equal total pre-sweep mature transparent "
            "({}); got {}".format(pre_transparent_total, result['shieldingValue']))
        # No `limit` parameter passed; nothing should remain.
        assert_equal(result['remainingUTXOs'], 0,
                     "remainingUTXOs should be 0 with no `limit`")
        assert_equal(Decimal(result['remainingValue']), Decimal('0'),
                     "remainingValue should be 0 with no `limit`")

        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(
            txid is not None,
            "Multi-receiver sweep must succeed; privacy policy must permit "
            "linking same-account transparent addresses.")
        print("  Shielding tx: {}".format(txid))

        # ---- Phase 6: confirm the sweep tx. ------------------------
        # Mining one block confirms the sweep tx (it lands in block 103).
        # It also produces a new coinbase output to B (block 3 reaches
        # 101 confirmations — i.e. just matures). That residual is from
        # this test-infrastructure confirmation block, NOT a missed
        # sweep input — block 1 and block 2 have both been spent.
        node.generate(1)

        # Wait for shielded balance to register the sweep.
        deadline = time.time() + 120
        post_private = Decimal('0')
        while time.time() < deadline:
            post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
            if post_private > 0:
                break
            time.sleep(1)

        # ---- Phase 7: post-sweep assertions (exact). ---------------
        # Shielded balance equals the swept value minus the fee paid
        # by the shielding tx. Read the fee directly off the tx.
        tx_details = w0.z_viewtransaction(txid)
        fee = Decimal(tx_details['fee'])
        assert_true(fee > 0, "Sweep fee should be positive, got {}".format(fee))
        assert_equal(
            post_private,
            Decimal(result['shieldingValue']) - fee,
            "Post-sweep shielded balance should equal shieldingValue ({}) - fee ({}); "
            "got {}".format(result['shieldingValue'], fee, post_private))

        # Block 1 (A's only mature coinbase) was spent in the sweep:
        # no mature transparent UTXOs on A remain.
        post_mature_a = self._mature_coinbase_on(w0, taddr_a)
        assert_equal(
            len(post_mature_a), 0,
            "Post-sweep: expected 0 mature coinbase on A (block 1 was spent), "
            "saw {}".format(len(post_mature_a)))

        # On B, block 2 was spent — but mining the confirmation block
        # advanced the tip by one, which brought block 3 to 101
        # confirmations. The single remaining UTXO is that block 3
        # coinbase, not a sweep residual.
        post_mature_b = self._mature_coinbase_on(w0, taddr_b)
        assert_equal(
            len(post_mature_b), 1,
            "Post-sweep: expected exactly 1 mature coinbase on B (the "
            "confirmation block's coinbase, not a sweep residual), saw {}".format(
                len(post_mature_b)))

        print("PASSED ({} ZEC swept; {} ZEC shielded after {} ZEC fee)".format(
            pre_transparent_total, post_private, fee))


if __name__ == '__main__':
    WalletZShieldCoinbaseMultiTaddrTest().main()
