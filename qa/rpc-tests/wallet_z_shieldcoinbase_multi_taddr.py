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
# `mining.miner_address` mid-test, which means a stop+restart.
#
# The wallet's SQLite block records must not end up referencing a chain state
# that zebrad no longer has after the restart, or the wallet upserts conflicting
# block hashes (`SqliteClientError::BlockConflict`) or fails history recovery
# fetching transactions that no longer exist. The robust way to guarantee that
# is to keep the pre-restart chain exactly ONE block long:
#   1. `prepare_chain` mines block 1 (coinbase to A); the wallet syncs to it.
#   2. Wait for zebrad to persist block 1 to its non-finalized-state backup —
#      the only thing that carries a non-finalized block across a restart, since
#      MAX_BLOCK_REORG_HEIGHT = 1000 leaves block 1 far from finalized.
#   3. Stop zallet and zebrad, restart zebrad with `mining.miner_address = B`.
#      zebrad restores exactly block 1, so the wallet's single scanned block is
#      unchanged and mining at B only EXTENDS the chain — no height the wallet
#      holds is rewritten, so no truncate and no conflict.
#   4. Mine coinbase to B until both A's (block 1) and B's (block 2) coinbase
#      outputs are mature.
#   5. Sweep via the UUID form and assert the sweep covered coinbase across
#      BOTH receivers.
#

import os
import time

from decimal import Decimal
from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs
from test_framework.util import (
    COINBASE_MATURITY,
    COINBASE_SETTLE_SECS,
    TotalBalanceField,
    assert_equal,
    assert_shieldcoinbase_preflight_shape,
    assert_true,
    first_transparent_receiver,
    mature_coinbase_on_address,
    node_dir,
    nu_activation_all_at_1,
    start_node,
    start_nodes,
    start_wallets,
    stop_node,
    stop_wallets,
    wait_and_assert_operationid_status,
    wait_zebrads,
    wait_for_total_balance,
    wait_for_tx_scanned,
    wait_zallets,
)


class WalletZShieldCoinbaseMultiTaddrTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        # All later NUs must also be listed at height 1; otherwise zebra mines
        # a coinbase committing to NU5's consensus branch ID while zallet's
        # network params expect the latest NU's branch ID, and zallet rejects
        # the coinbase on the first block sync. See ZebraArgs default in
        # test_framework/config.py.
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights=nu_activation_all_at_1(),
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def run_test(self):
        node = self.nodes[0]
        w0 = self.wallets[0]
        # `prepare_wallets_for_mining` captures the raw zallet CLI
        # stdout; normalize for string comparison against
        # `z_listunspent`'s canonical encoding.
        taddr_a = self.miner_addresses[0].strip()

        # Wait for the wallet to sync to the node tip. z_getaddressforaccount
        # (and other account RPCs) reject with "chain height is unknown" until
        # the wallet has committed at least one block, since they anchor the
        # address derivation to a chain height.
        self.sync_all()

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

        # ---- Phase 1: persist block 1 (A's coinbase) for the restart. ----
        # `prepare_chain` mined block 1 (coinbase to A) and run_test synced the
        # wallet to it. We deliberately mine NOTHING more before the miner
        # switch: keeping the pre-restart chain exactly one block long means the
        # wallet never scans a height that zebrad won't still have after the
        # restart, so there are no orphaned wallet records to conflict or to
        # choke history recovery.
        #
        # MAX_BLOCK_REORG_HEIGHT = 1000, so block 1 is nowhere near finalized;
        # zebrad carries it across the restart only via its non-finalized-state
        # backup, written at most every MIN_DURATION_BETWEEN_BACKUP_UPDATES (5s)
        # and NOT flushed on clean shutdown. Wait for block 1's backup file to
        # appear (named by block hash, in display order) before stopping, so the
        # restored chain is guaranteed to contain it.
        assert_equal(node.getblockcount(), 1,
                     "Expected exactly block 1 before the miner switch")
        block1_hash = node.getbestblockhash()
        backup_dir = os.path.join(
            node_dir(self.options.tmpdir, 0), "non_finalized_state", "regtest")
        print("Waiting for zebra to back up block 1 ({}...)".format(block1_hash[:16]))
        deadline = time.time() + 60
        while time.time() < deadline:
            if os.path.exists(os.path.join(backup_dir, block1_hash)):
                break
            time.sleep(0.5)
        else:
            raise AssertionError(
                "zebra did not persist block 1 to its non-finalized backup "
                "within 60s; cannot guarantee it survives the restart")

        # ---- Phase 2: stop, switch miner to B, restart. ----
        # No wallet truncate and no zcash/wallet#447 workaround are needed: the
        # wallet has only scanned block 1, which zebrad restores unchanged, so
        # mining at B only EXTENDS the chain (blocks 2+). No height the wallet
        # already holds is rewritten -> no BlockConflict, and no orphaned
        # transaction for history recovery to fetch.
        print("Stopping wallet and node before the miner switch...")
        stop_wallets(self.wallets)
        wait_zallets()
        stop_node(self.nodes[0], 0)
        wait_zebrads()

        print("Restarting zebrad with miner=B...")
        self.nodes[0] = start_node(
            0, self.options.tmpdir,
            ZebraArgs(miner_address=taddr_b,
                      activation_heights=nu_activation_all_at_1()))
        node = self.nodes[0]
        # zebra must have restored exactly block 1 (A's coinbase). If the backup
        # had not captured it, the restored tip would be genesis and the premise
        # (one preserved A coinbase) would be invalid — fail loudly rather than
        # silently re-mining height 1 under miner B.
        assert_equal(node.getblockcount(), 1,
                     "Post-restart zebra should have restored exactly block 1")
        self.wallets = start_wallets(self.num_wallets, self.options.tmpdir)
        w0 = self.wallets[0]

        # ---- Phase 3: mine to B until exactly one B coinbase matures.
        # Post-restart zebrad has only block 1. Mining COINBASE_MATURITY more
        # → tip 101: block 1 (A) and block 2 (B) reach maturity (101 / 100
        # confirmations), block 3 (B) does not (99). So one mature coinbase
        # per receiver, and the UUID sweep selects exactly those two.
        print("Mining {} blocks at miner=B...".format(COINBASE_MATURITY))
        node.generate(COINBASE_MATURITY)

        # Wait for the 1+1 state to hold for COINBASE_SETTLE_SECS (see above).
        print("Waiting for wallet to see exactly 1 mature coinbase on each receiver...")
        deadline = time.time() + 240
        mature_a = []
        mature_b = []
        stable_secs = 0
        while time.time() < deadline:
            mature_a = mature_coinbase_on_address(w0, taddr_a)
            mature_b = mature_coinbase_on_address(w0, taddr_b)
            if len(mature_a) == 1 and len(mature_b) == 1:
                stable_secs += 1
                if stable_secs >= COINBASE_SETTLE_SECS:
                    break
            else:
                stable_secs = 0
            time.sleep(1)

        # ---- Phase 4: pre-sweep assertions (exact). ----------------
        # Expect 1 mature coinbase on each receiver: block 1 (A) at
        # 101 confirmations, block 2 (B) at 100 confirmations.
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
        assert_shieldcoinbase_preflight_shape(result)

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

        # Pre-sweep coinbase UTXOs on each receiver; checked spent below.
        spent_a = (mature_a[0]['txid'], mature_a[0]['outindex'])
        spent_b = (mature_b[0]['txid'], mature_b[0]['outindex'])

        # ---- Phase 6: confirm the sweep tx. ------------------------
        # Wait for the confirming block to be scanned (the tx view carries a
        # fee), then poll the balance to reach exactly shieldingValue - fee.
        # z_gettotalbalance's summary tip can lag the scanned tx by a block, so
        # this is both the sync barrier and the exact assertion.
        node.generate(1)
        shielding_value = Decimal(result['shieldingValue'])
        fee = Decimal(wait_for_tx_scanned(w0, txid)['fee'])
        expected_private = shielding_value - fee
        post_private = wait_for_total_balance(
            w0, TotalBalanceField.PRIVATE, lambda v: v == expected_private)

        # ---- Phase 7: post-sweep assertions (exact). ---------------
        assert_true(fee > 0,
                    "Sweep fee should be positive, got {}".format(fee))
        assert_equal(
            post_private,
            expected_private,
            "Post-sweep shielded balance should equal shieldingValue ({}) - fee ({}); "
            "got {}".format(shielding_value, fee, post_private))

        # Both receivers' coinbase must be spent by the one UUID sweep — the
        # multi-receiver regression guard (a skipped receiver B would remain).
        unspent_ids = {
            (u['txid'], u['outindex'])
            for u in w0.z_listunspent(1)
            if u.get('pool') == 'transparent'
        }
        assert_true(
            spent_a not in unspent_ids,
            "Receiver A's coinbase (block 1) must be spent by the sweep")
        assert_true(
            spent_b not in unspent_ids,
            "Receiver B's coinbase (block 2) must be spent by the sweep")

        print("PASSED ({} ZEC swept from 2 receivers; {} ZEC shielded after {} ZEC fee)".format(
            pre_transparent_total, post_private, fee))


if __name__ == '__main__':
    WalletZShieldCoinbaseMultiTaddrTest().main()
