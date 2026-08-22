#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Note locking is scoped to the account that locked (zallet + librustzcash
# note locking):
#
#   account_isolation: locks held for account A never change account B's
#   spendable balance, selection, or z_clearlockedoutputs behavior. With A's
#   notes locked (a crashed in-flight send), B's balance is untouched, B can
#   spend, clearing B's (nonexistent) locks is a no-op that does not release
#   A's, and clearing A's locks restores exactly A.
#
# The stale-lock state is produced by the crash pattern of
# wallet_note_locking_crash.py: SIGKILL right after the locking send's
# operation id returns, so the locks persist and nothing races the
# assertions. The second account is created before a wallet restart because
# zallet's scanner only tracks accounts that existed when it started (see
# wallet_ironwood_spending.py).
#

from decimal import Decimal

from test_framework.note_locking_common import NoteLockingScenario
from test_framework.util import (
    Pool,
    account_locked_zat,
    account_spendable_zat,
    assert_equal,
    assert_true,
    self_send,
    stop_wallets,
    wait_account_settled,
    wait_zallets,
)
from test_framework.util_ironwood import shield_coinbase_into_ironwood

# The amount of the send whose operation is crashed on account A, and of the
# probe send from account B. Small relative to the funded notes.
SEND_ZEC = Decimal('1')


class WalletNoteLockingIsolationTest(NoteLockingScenario):

    def graceful_restart(self):
        """Stop zallet cleanly and start it again on the persisted wallet DB,
        so the scanner picks up accounts created since the last start."""
        stop_wallets(self.wallets)
        wait_zallets()
        return self.restart_zallet()

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        if self.skip_if_locking_absent(w):
            return

        acct_a = w.z_listaccounts()[0]['account_uuid']
        # The scanner only tracks accounts that existed when zallet started,
        # so create B and restart before funding it.
        acct_b = w.z_getnewaccount('note-locking-b')['account_uuid']
        w = self.graceful_restart()

        ua_a = w.z_getaddressforaccount(acct_a, ['orchard'])['address']
        ua_b = w.z_getaddressforaccount(acct_b, ['orchard'])['address']

        print("Funding both accounts (two coinbase shields)...")
        _, funded_a = shield_coinbase_into_ironwood(node, w, taddr, ua_a,
                                                    acct_a)
        _, funded_b = shield_coinbase_into_ironwood(node, w, taddr, ua_b,
                                                    acct_b)
        wait_account_settled(w, acct_a)
        wait_account_settled(w, acct_b)
        print("  A holds {} zat, B holds {} zat".format(funded_a, funded_b))

        print("Scenario: A's locks never touch B...")
        # Crash a send from A mid-operation, leaving A's note locked.
        self.send_async(w, ua_a, ua_a, SEND_ZEC)
        self.crash_zallet()
        w = self.restart_zallet()

        locked_a = account_locked_zat(w, acct_a, Pool.IRONWOOD)
        assert_true(locked_a > 0,
                    "A's crashed operation left its note locked "
                    "(locked={} zat)".format(locked_a))

        # B is untouched: nothing locked, full balance spendable.
        assert_equal(0, account_locked_zat(w, acct_b, Pool.IRONWOOD),
                     "A's locks do not appear on B")
        assert_equal(funded_b,
                     account_spendable_zat(w, acct_b, Pool.IRONWOOD),
                     "B's spendable balance is unchanged by A's locks")

        # B can select and spend while A is locked.
        txid_b, fee_b = self_send(node, w, ua_b, SEND_ZEC)
        wait_account_settled(w, acct_b)
        print("  B sent {} (fee {}) while A was locked.".format(
            txid_b[:16], fee_b))

        # Clearing B's locks is a no-op scoped to B: it releases nothing and
        # does not touch A's locks.
        cleared_b = w.z_clearlockedoutputs(acct_b)
        assert_equal(0, cleared_b['cleared'],
                     "B has no locks to clear")
        assert_equal(locked_a, account_locked_zat(w, acct_a, Pool.IRONWOOD),
                     "clearing B leaves A's locks in place")

        # Clearing A restores exactly A.
        cleared_a = w.z_clearlockedoutputs(acct_a)
        assert_true(cleared_a['cleared'] >= 1,
                    "clearing A releases its stale locks "
                    "(cleared={})".format(cleared_a['cleared']))
        spendable_a = self.wait_for_unlocked_spendable(
            w, acct_a, Pool.IRONWOOD, min_zat=funded_a)
        assert_equal(funded_a, spendable_a,
                     "A's full balance is spendable again after clearing")

        txid_a, fee_a = self_send(node, w, ua_a, SEND_ZEC)
        wait_account_settled(w, acct_a)
        print("  cleared A ({} lock(s)); A sent {} (fee {}). PASSED".format(
            cleared_a['cleared'], txid_a[:16], fee_a))


if __name__ == '__main__':
    WalletNoteLockingIsolationTest().main()
