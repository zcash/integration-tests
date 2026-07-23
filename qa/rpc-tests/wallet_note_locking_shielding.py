#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Note locking for the transparent/shielding path (zallet + librustzcash
# note locking):
#
#   shielding_with_locking: a z_shieldcoinbase operation locks the coinbase
#   UTXOs it selected, so a concurrent shielding of the same UTXOs cannot
#   double-spend them: the second call fails with the retryable conflict
#   error, exactly one shielding transaction is broadcast, and once it is
#   stored the transparent locks are released (no lock lingers after the
#   sweep).
#
# This mirrors the concurrent-sends scenario of wallet_note_locking.py for
# the transparent input path: z_shieldcoinbase locks its inputs at proposal
# time (synchronously, before the RPC returns), and the pre-flight
# `remainingUTXOs` accounting keeps its pre-lock meaning (the proposal's own
# locked selection still counts as eligible).
#

from test_framework.note_locking_common import (
    NoteLockingScenario,
    assert_conflict_error,
)
from test_framework.util import (
    Pool,
    account_locked_zat,
    assert_equal,
    assert_true,
    expect_rpc_error,
    mine_to_mature_coinbase,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
)


class WalletNoteLockingShieldingTest(NoteLockingScenario):

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        if self.skip_if_locking_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']

        print("Mining coinbase to maturity...")
        utxos = mine_to_mature_coinbase(node, w)
        assert_true(len(utxos) > 0, "mature coinbase UTXOs are available")

        print("Scenario: concurrent shieldings cannot double-spend UTXOs...")
        # The first shielding selects (and locks) every eligible coinbase
        # UTXO; the RPC returns after the proposal exists, with the sweep
        # building in the background.
        first = w.z_shieldcoinbase(taddr, ua)
        assert_equal(0, first['remainingUTXOs'],
                     "the first shielding selected every eligible UTXO "
                     "(remainingUTXOs keeps its pre-lock meaning)")

        # While it is in flight, its transparent inputs are locked.
        locked = account_locked_zat(w, acct, Pool.TRANSPARENT)
        assert_true(locked > 0,
                    "the in-flight shielding's UTXOs are locked "
                    "(locked={} zat)".format(locked))

        # A concurrent shielding of the same UTXOs fails with the retryable
        # conflict error instead of building a second sweep over them.
        e = expect_rpc_error(w.z_shieldcoinbase, taddr, ua)
        assert_conflict_error(e)

        # Exactly one shielding transaction is broadcast.
        txid = wait_and_assert_operationid_status(w, first['opid'])
        mempool = node.getrawmempool()
        assert_true(txid in mempool, "the first sweep was broadcast")
        assert_equal(1, len(mempool),
                     "only the first sweep reaches the mempool")

        node.generate(1)
        wait_for_tx_scanned(w, txid)

        # Once the sweep is stored, the transparent locks are gone (the
        # inputs are spent, not locked) and the shielded value lands.
        assert_equal(0, account_locked_zat(w, acct, Pool.TRANSPARENT),
                     "no transparent lock lingers after the sweep")
        ironwood_zat = wait_for_account_spendable(w, acct, Pool.IRONWOOD)
        assert_true(ironwood_zat > 0, "the swept value is spendable")
        print("  second sweep rejected ({!r}); one tx mined; {} zat shielded. "
              "PASSED".format(e.error['message'][:60], ironwood_zat))


if __name__ == '__main__':
    WalletNoteLockingShieldingTest().main()
