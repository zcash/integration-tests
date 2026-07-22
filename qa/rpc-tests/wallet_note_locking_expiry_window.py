#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# The documented lock-expiry trade-off, under a deliberately tiny window
# (zallet + librustzcash note locking):
#
#   short_lock_window_selfheals: with `builder.note_lock_blocks = 1`, a lock
#   outlives its operation only if the operation finishes within one block.
#   When the operation does NOT finish in time (here: it is crashed), the
#   lock expires as soon as the chain advances past the window, and a second
#   send may take the very same inputs. The system self-heals: at most one
#   transaction spending any given note ever mines, and the wallet's final
#   balance is consistent.
#
# This is the documented trade-off of height-based expiry: a window shorter
# than the worst-case build-and-prove time re-opens the race that locking
# exists to prevent. The window exists so a crashed wallet's funds are never
# stuck forever; choosing it comfortably above proving time (the default 40
# blocks) is what keeps the race closed in practice.
#
# APPROXIMATION: the ideal form of this scenario would artificially DELAY the
# first operation's build (so both the delayed original and the post-expiry
# second send broadcast, and the chain picks one). Zallet has no failure- or
# latency-injection hook for the builder, so the closest reachable form
# crashes the first operation instead: the expiry and re-selection of the
# same inputs are exercised for real, while the "two competing broadcasts,
# one mines" tail is covered by construction (the crashed operation never
# broadcasts, so consistency is trivially preserved).
# TODO(zallet): revisit with a build-delay injection hook if one is added.
#

from decimal import Decimal

from test_framework.note_locking_common import NoteLockingScenario
from test_framework.util import (
    COIN,
    Pool,
    account_locked_zat,
    assert_equal,
    assert_true,
    self_send,
    wait_account_settled,
)
from test_framework.util_ironwood import shield_coinbase_into_ironwood

# The deliberately tiny lock window under test: one block, far below any
# realistic build-and-prove time.
SHORT_LOCK_BLOCKS = 1

# The amount of the crashed and follow-up sends. Small relative to the
# funded note.
SEND_ZEC = Decimal('1')


class WalletNoteLockingExpiryWindowTest(NoteLockingScenario):

    NOTE_LOCK_BLOCKS = SHORT_LOCK_BLOCKS

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

        print("Scenario: a 1-block lock window expires and self-heals...")
        # Start a locking send and crash it mid-build (the stand-in for an
        # operation that outlives its own lock window; see the module
        # comment). Its lock covers heights up to target + 1 only.
        self.send_async(w, ua, ua, SEND_ZEC)
        self.crash_zallet()
        w = self.restart_zallet()

        locked = account_locked_zat(w, acct, Pool.IRONWOOD)
        assert_true(locked > 0,
                    "the crashed operation's short-window lock is present "
                    "(locked={} zat)".format(locked))

        # Two blocks push the target height past the 1-block window; the
        # lock expires with no clearing and no waiting out a long default.
        node.generate(SHORT_LOCK_BLOCKS + 1)
        spendable = self.wait_for_unlocked_spendable(
            w, acct, Pool.IRONWOOD, min_zat=funded_zat)
        assert_equal(funded_zat, spendable,
                     "the short-window lock expired on its own")

        # A second send now takes the very same inputs the crashed operation
        # had locked; only this one ever broadcasts and mines.
        assert_equal([], node.getrawmempool(),
                     "the crashed operation broadcast nothing")
        txid, fee = self_send(node, w, ua, SEND_ZEC)
        wait_account_settled(w, acct)

        # The final balance is consistent: exactly one transaction spent the
        # note, so only its fee left the account.
        final = funded_zat - int(fee * COIN)
        assert_equal(
            final,
            self.wait_for_unlocked_spendable(w, acct, Pool.IRONWOOD,
                                             min_zat=final),
            "exactly one transaction spent the re-selected inputs")
        print("  lock expired after {} block(s); send {} (fee {}) took the "
              "same inputs; final balance consistent. PASSED".format(
                  SHORT_LOCK_BLOCKS + 1, txid[:16], fee))


if __name__ == '__main__':
    WalletNoteLockingExpiryWindowTest().main()
