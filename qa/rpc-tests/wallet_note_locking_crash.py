#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Note locking across a wallet crash (zallet + librustzcash note locking):
#
#   1. locks_survive_restart: locks live in the wallet's SQLite database, so
#      a zallet killed mid-operation still reports the locked balance after a
#      restart.
#   2. clear_locks_recovery: z_clearlockedoutputs releases a crashed
#      operation's stale locks, restoring spendability without waiting for
#      expiry.
#   3. lock_expiry_restores_funds: stale locks also expire on their own once
#      the chain advances past the lock window (`builder.note_lock_blocks`,
#      40 by default), restoring spendability with no manual intervention.
#
# The crash is a SIGKILL delivered right after z_sendmany returns its
# operation id: the proposal (and its locks) is created synchronously inside
# the RPC call, and the kill lands while the operation is still building and
# proving in the background, before the transactions are stored, so no unlock
# runs. This is also the closest reachable approximation of "a proposal was
# created and then abandoned without unlocking": zallet's RPC surface has no
# way to create a proposal without also driving it to a transaction.
#
# TODO(zallet): "a persisted in-flight PCZT/proposal can still be completed
# after restart" (the locked-proposal decode fix in librustzcash PR #2726,
# `proposal_from_standard_proposal` decoding proposals without filtering
# locked inputs) is not reachable through zallet: async operations are held
# in memory only, and no RPC persists or resumes a proposal/PCZT for a plain
# send. Once such a surface exists, extend this test to complete a persisted
# proposal after the restart.
#

from decimal import Decimal

from test_framework.note_locking_common import (
    DEFAULT_NOTE_LOCK_BLOCKS,
    NoteLockingScenario,
)
from test_framework.util import (
    COIN,
    Pool,
    account_balance_zat,
    account_locked_zat,
    account_spendable_zat,
    assert_equal,
    assert_true,
    self_send,
    wait_account_settled,
)
from test_framework.util_ironwood import shield_coinbase_into_ironwood

# The amount of the sends whose operations are crashed. Small relative to the
# funded note so a single-note selection always covers amount + fee.
CRASH_SEND_ZEC = Decimal('1')


class WalletNoteLockingCrashTest(NoteLockingScenario):

    def crash_mid_send(self, w, ua):
        """Start a locking send and SIGKILL zallet before its operation can
        store its transactions, leaving the proposal's locks stale in the
        wallet database. Returns the restarted wallet handle."""
        self.send_async(w, ua, ua, CRASH_SEND_ZEC)
        # Kill immediately: the locks were taken synchronously before the
        # opid came back, and building/proving the transaction takes long
        # enough that the kill lands before the operation stores it.
        self.crash_zallet()
        return self.restart_zallet()

    def scenario_locks_survive_restart(self, w, acct, funded_zat):
        """Locks live in SQLite: after a crash mid-operation and a restart,
        the locked balance is still reported and still excluded from the
        spendable balance."""
        print("Scenario 1: locks survive a wallet restart...")
        locked = account_locked_zat(w, acct, Pool.IRONWOOD)
        spendable = account_spendable_zat(w, acct, Pool.IRONWOOD)
        total = account_balance_zat(w, acct, Pool.IRONWOOD)
        assert_true(locked > 0,
                    "the crashed operation's locks survive the restart "
                    "(locked={} zat)".format(locked))
        assert_equal(funded_zat, total,
                     "the locked value still belongs to the account")
        assert_equal(total - locked, spendable,
                     "locked value stays out of the spendable balance")
        print("  {} zat still locked after restart. PASSED".format(locked))
        return locked

    def scenario_clear_locks_recovery(self, node, w, ua, acct, funded_zat):
        """z_clearlockedoutputs releases a crashed operation's stale locks,
        restoring spendability immediately; a follow-up send succeeds."""
        print("Scenario 2: z_clearlockedoutputs recovers the stale locks...")
        result = w.z_clearlockedoutputs(acct)
        assert_equal(acct, result['account_uuid'])
        assert_true(result['cleared'] >= 1,
                    "clearing released the crashed operation's locks "
                    "(cleared={})".format(result['cleared']))

        spendable = self.wait_for_unlocked_spendable(
            w, acct, Pool.IRONWOOD, min_zat=funded_zat)
        assert_equal(funded_zat, spendable,
                     "the full balance is spendable again after clearing")

        txid, fee = self_send(node, w, ua, CRASH_SEND_ZEC)
        wait_account_settled(w, acct)
        print("  cleared {} lock(s); follow-up send {} (fee {}) succeeded. "
              "PASSED".format(result['cleared'], txid[:16], fee))
        return fee

    def scenario_lock_expiry_restores_funds(self, node, w, ua, acct,
                                            balance_zat):
        """Stale locks expire on their own once the chain advances past the
        lock window; the funds become spendable with no manual intervention
        and a new send succeeds."""
        print("Scenario 3: stale locks expire as the chain advances...")
        w = self.crash_mid_send(w, ua)
        assert_true(account_locked_zat(w, acct, Pool.IRONWOOD) > 0,
                    "the second crashed operation left stale locks")

        # The crashed operation never broadcast, so nothing is in flight; the
        # only thing holding the funds is the stale lock, which expires once
        # the chain passes `target_height + note_lock_blocks`.
        assert_equal([], node.getrawmempool(),
                     "the crashed operation broadcast nothing")
        node.generate(DEFAULT_NOTE_LOCK_BLOCKS + 1)

        spendable = self.wait_for_unlocked_spendable(
            w, acct, Pool.IRONWOOD, min_zat=balance_zat)
        assert_equal(balance_zat, spendable,
                     "the full balance is spendable again after expiry")

        txid, fee = self_send(node, w, ua, CRASH_SEND_ZEC)
        wait_account_settled(w, acct)
        print("  locks expired after {} blocks; send {} (fee {}) succeeded. "
              "PASSED".format(DEFAULT_NOTE_LOCK_BLOCKS + 1, txid[:16], fee))

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

        # Crash cycle 1: restart visibility, then explicit recovery.
        w = self.crash_mid_send(w, ua)
        self.scenario_locks_survive_restart(w, acct, funded_zat)
        fee1 = self.scenario_clear_locks_recovery(node, w, ua, acct,
                                                 funded_zat)

        # Crash cycle 2: recovery by expiry alone. The balance carried into
        # it is the funded value minus the one successful send's fee.
        balance_zat = funded_zat - int(fee1 * COIN)
        self.scenario_lock_expiry_restores_funds(node, w, ua, acct,
                                                 balance_zat)


if __name__ == '__main__':
    WalletNoteLockingCrashTest().main()
