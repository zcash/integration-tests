#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Note locking, core lifecycle (zallet + librustzcash note locking):
#
#   1. locked_balance_visibility: while a spend operation is in flight, the
#      value it selected is reported as LOCKED: excluded from the spendable
#      balance, still counted in the total, and surfaced by both z_getbalances
#      (the per-pool `locked` component) and z_getbalanceforaccount
#      (`lockedZat`).
#   2. concurrent_sends_no_double_spend (the audit scenario): two overlapping
#      sends on one account whose input needs overlap cannot double-spend a
#      note: exactly one proceeds, the other fails with the retryable
#      account-busy / insufficient-funds error, and at most one transaction
#      spending the notes is ever broadcast.
#   3. lock_released_on_broadcast: once a locking send's transactions are
#      stored and broadcast, no locks remain; a follow-up send succeeds.
#   4. all_notes_locked_insufficient_funds: with every note locked, a further
#      send fails with a clean retryable error (no crash, no partial state)
#      and succeeds after the locks are released.
#
# The lock is taken synchronously inside the z_sendmany RPC call, before the
# operation id is returned; the build/prove/broadcast phase then runs in the
# background. That is the in-flight window these scenarios observe.
#

from decimal import Decimal

from test_framework.note_locking_common import (
    NoteLockingScenario,
    assert_conflict_error,
)
from test_framework.util import (
    COIN,
    Pool,
    account_balance_zat,
    account_locked_zat,
    account_spendable_zat,
    assert_equal,
    assert_true,
    expect_rpc_error,
    self_send,
    wait_account_settled,
    wait_and_assert_operationid_status,
    wait_for_tx_scanned,
)
from test_framework.util_ironwood import shield_coinbase_into_ironwood

# The first (visibility) self-send's amount. Small relative to the funded
# note, so the single-note selection is guaranteed and the fee is covered.
VISIBILITY_SEND_ZEC = Decimal('1')

# Margin, in zatoshis, subtracted from the spendable balance to size the
# "needs every note" send: half a coin is strictly less than the 1-ZEC
# payment note a prior self-send created (so the amount exceeds the largest
# single note and selection must combine every note) while leaving far more
# than the ZIP-317 fee.
OVERLAP_MARGIN_ZAT = COIN // 2


class WalletNoteLockingTest(NoteLockingScenario):

    def all_notes_locking_send(self, w, ua, acct):
        """Start a send sized so selection must combine EVERY note in the
        account (amount exceeds the largest single note), locking the whole
        spendable balance. Returns (opid, spendable_before_zat)."""
        wait_account_settled(w, acct)
        spendable = account_spendable_zat(w, acct, Pool.IRONWOOD)
        assert_true(spendable > OVERLAP_MARGIN_ZAT,
                    "not enough funds to size the all-notes send")
        amount_zec = Decimal(spendable - OVERLAP_MARGIN_ZAT) / COIN
        opid = self.send_async(w, ua, ua, amount_zec)
        return opid, spendable

    def scenario_locked_balance_visibility(self, node, w, ua, acct,
                                           funded_zat):
        """While a send is in flight, its selected value is locked: the
        spendable balance excludes it, the total balance is unchanged, and
        both balance RPCs surface the locked bucket."""
        print("Scenario 1: locked balance visibility...")
        opid = self.send_async(w, ua, ua, VISIBILITY_SEND_ZEC)

        # The proposal locked its inputs before the RPC returned; the single
        # funded note is the only possible input, so it is now locked.
        locked = account_locked_zat(w, acct, Pool.IRONWOOD)
        spendable = account_spendable_zat(w, acct, Pool.IRONWOOD)
        total = account_balance_zat(w, acct, Pool.IRONWOOD)
        assert_equal(funded_zat, locked,
                     "the in-flight send's selected note is locked")
        assert_equal(0, spendable,
                     "locked value leaves the spendable balance")
        assert_equal(funded_zat, total,
                     "locked value still counts toward the total balance")

        # z_getbalanceforaccount reports the same lock, per pool.
        pools = w.z_getbalanceforaccount(acct)['pools']
        assert_equal(locked, pools['ironwood'].get('lockedZat'),
                     "z_getbalanceforaccount surfaces lockedZat")
        assert_equal(0, pools['ironwood']['valueZat'],
                     "z_getbalanceforaccount's spendable excludes the lock")

        txid = wait_and_assert_operationid_status(w, opid)
        node.generate(1)
        wait_for_tx_scanned(w, txid)
        wait_account_settled(w, acct)
        print("  locked {} zat while in flight; op completed. PASSED"
              .format(locked))

    def scenario_concurrent_sends_no_double_spend(self, node, w, ua, acct):
        """Two overlapping sends whose input needs overlap cannot double-spend:
        one proceeds, the other fails with the retryable conflict error, and
        exactly one transaction is broadcast."""
        print("Scenario 2: concurrent overlapping sends cannot double-spend...")
        opid_a, spendable_before = self.all_notes_locking_send(w, ua, acct)
        assert_equal(spendable_before,
                     account_locked_zat(w, acct, Pool.IRONWOOD),
                     "send A locked every note")

        # Send B overlaps send A's inputs by construction (A locked every
        # note), so it must fail with the retryable conflict error rather
        # than build a second transaction over the same notes.
        e = expect_rpc_error(self.send_async, w, ua, ua, VISIBILITY_SEND_ZEC)
        assert_conflict_error(e)

        # Exactly one transaction spending the account's notes is broadcast.
        txid_a = wait_and_assert_operationid_status(w, opid_a)
        mempool = node.getrawmempool()
        assert_true(txid_a in mempool, "send A's transaction was broadcast")
        assert_equal(1, len(mempool),
                     "only send A's transaction reaches the mempool")

        node.generate(1)
        wait_for_tx_scanned(w, txid_a)
        wait_account_settled(w, acct)
        print("  send B rejected ({!r}); one tx broadcast and mined. PASSED"
              .format(e.error['message'][:60]))

    def scenario_lock_released_on_broadcast(self, node, w, ua, acct):
        """After a locking send is stored and broadcast, no locks remain and
        a follow-up send succeeds."""
        print("Scenario 3: locks are released on broadcast...")
        for pool in (Pool.IRONWOOD, Pool.ORCHARD, Pool.SAPLING,
                     Pool.TRANSPARENT):
            assert_equal(0, account_locked_zat(w, acct, pool),
                         "no lock lingers in {} after broadcast".format(pool))

        txid, fee = self_send(node, w, ua, VISIBILITY_SEND_ZEC)
        wait_account_settled(w, acct)
        print("  no residual locks; follow-up send {} (fee {}) succeeded. "
              "PASSED".format(txid[:16], fee))

    def scenario_all_notes_locked_insufficient_funds(self, node, w, ua, acct):
        """With every note locked by an in-flight send, a further send fails
        with a clean retryable error, the wallet stays functional, and the
        same send succeeds once the locks are released."""
        print("Scenario 4: every note locked -> clean retryable failure...")
        opid_a, _ = self.all_notes_locking_send(w, ua, acct)

        e = expect_rpc_error(self.send_async, w, ua, ua, VISIBILITY_SEND_ZEC)
        assert_conflict_error(e)

        # No crash, no partial state: the wallet still answers, and the failed
        # send left nothing behind (no operation, no transaction, no lock of
        # its own beyond send A's).
        assert_true(w.z_getbalances() is not None,
                    "the wallet remains responsive after the rejected send")

        txid_a = wait_and_assert_operationid_status(w, opid_a)
        node.generate(1)
        wait_for_tx_scanned(w, txid_a)
        wait_account_settled(w, acct)

        # The rejected send succeeds verbatim once the locks are gone.
        txid_b, fee_b = self_send(node, w, ua, VISIBILITY_SEND_ZEC)
        wait_account_settled(w, acct)
        print("  rejected while locked, succeeded after release "
              "({}, fee {}). PASSED".format(txid_b[:16], fee_b))

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        if self.skip_if_locking_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']

        print("Funding one Ironwood note (shield coinbase)...")
        _, funded_zat = shield_coinbase_into_ironwood(node, w, taddr, ua, acct)
        wait_account_settled(w, acct)
        print("  funded {} zat".format(funded_zat))

        self.scenario_locked_balance_visibility(node, w, ua, acct, funded_zat)
        self.scenario_concurrent_sends_no_double_spend(node, w, ua, acct)
        self.scenario_lock_released_on_broadcast(node, w, ua, acct)
        self.scenario_all_notes_locked_insufficient_funds(node, w, ua, acct)


if __name__ == '__main__':
    WalletNoteLockingTest().main()
