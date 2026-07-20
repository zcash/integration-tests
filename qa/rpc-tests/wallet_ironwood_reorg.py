#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) reorg / commitment-tree rollback, against the Z3
# stack (zebrad + zaino + zallet).
#
# When a block that created an Ironwood note is disconnected by a reorg, the
# wallet must roll back its Ironwood note-commitment tree along with the note:
# the note stops being spendable, and reconnecting the block restores it. This
# is exactly the shardtree/checkpoint path that Ironwood stresses.
#
# CURRENTLY DISABLED (see qa/pull-tester/rpc-tests.py DISABLED_SCRIPTS).
# Originally disabled pending zcash/zallet#556 ("steady_state sync crashes the
# whole process on BlockConflict, instead of using existing reorg-rewind
# recovery"), which zcash/zallet#560 fixed (merged 2026-07-08). That fix is
# NOT sufficient, though: re-verified locally against a zallet built from
# `main` @ d168efe (past #560, #563, and #576) on the zebra backend, and
# `wait_for_wallet_sync` still never converges after `invalidateblock` — the
# wallet does not appear to notice the node's tip shrank at all, which is a
# different failure mode than the BlockConflict crash #560 fixed (that path
# triggers on a *replacing* block at a conflicting height; a bare
# invalidateblock with nothing yet mined over it may never reach it). Filed as
# zcash/zallet#587. Re-enable once the wallet backends actually detect and
# follow a shrinking tip.
#
# The reorg is driven on a single node with invalidateblock / reconsiderblock
# (both served by zebra in regtest); the wallet follows its own node.
#

import time

from test_framework.util import (
    Pool,
    account_spendable_zat,
    assert_equal,
    assert_true,
    mine_to_mature_coinbase,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
    wait_for_wallet_sync,
)
from test_framework.util_ironwood import IronwoodTestFramework


def wait_spendable_below(w, acct, pool, threshold: int,
                         timeout: int = 120) -> int:
    """Block until the account's spendable `pool` balance drops below
    `threshold`, then return it."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = account_spendable_zat(w, acct, pool)
        if last < threshold:
            return last
        time.sleep(1)
    raise AssertionError(
        "spendable {} balance stayed at {} zat, never fell below {}".format(
            pool, last, threshold))


class WalletIronwoodReorgTest(IronwoodTestFramework):

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']

        # ---- Create an Ironwood note in a known block -------------------
        print("Shielding coinbase into an Ironwood note...")
        mine_to_mature_coinbase(node, w)
        result = w.z_shieldcoinbase(taddr, ua)
        txid = wait_and_assert_operationid_status(w, result['opid'])
        assert_true(txid is not None, "shield should succeed")

        node.generate(1)
        confirming_block = node.getbestblockhash()
        confirming_height = node.getblockcount()
        wait_for_tx_scanned(w, txid)

        # Bury the note a few blocks deep, then read the stable balance.
        node.generate(5)
        wait_for_wallet_sync(node, w)
        spendable = account_spendable_zat(w, acct, Pool.IRONWOOD)
        assert_true(spendable > 0, "Ironwood note should be spendable")
        print("  Ironwood spendable before reorg: {} zat".format(spendable))

        # ---- Disconnect the block: the note must roll back --------------
        print("Reorg: invalidate the block that created the Ironwood note...")
        node.invalidateblock(confirming_block)
        assert_true(node.getblockcount() < confirming_height,
                    "invalidateblock should lower the node tip")
        wait_for_wallet_sync(node, w)
        # The wallet must survive the Ironwood-tree rollback (no checkpoint
        # crash) and stop counting the un-mined note as spendable.
        rolled_back = wait_spendable_below(w, acct, Pool.IRONWOOD, spendable)
        assert_equal(rolled_back, 0,
                     "the un-mined Ironwood note must not be spendable")
        print("  Ironwood spendable after disconnect: {} zat. PASSED"
              .format(rolled_back))

        # ---- Reconnect the block: the note must return ------------------
        print("Reorg: reconsider the block; the Ironwood note must return...")
        node.reconsiderblock(confirming_block)
        assert_true(node.getblockcount() >= confirming_height,
                    "reconsiderblock should restore the chain")
        wait_for_wallet_sync(node, w)
        restored = wait_for_account_spendable(
            w, acct, Pool.IRONWOOD, min_zat=spendable)
        assert_equal(restored, spendable,
                     "reconnecting the block must restore the Ironwood note")
        assert_true('orchard' not in w.z_getbalanceforaccount(acct)['pools'],
                    "restored funds must be Ironwood, not Orchard")
        print("  Ironwood spendable after reconnect: {} zat. PASSED"
              .format(restored))

        print("\nAll Ironwood reorg tests passed!")


if __name__ == '__main__':
    WalletIronwoodReorgTest().main()
