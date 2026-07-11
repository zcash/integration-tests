#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) note selection and spending, against the Z3 stack
# (zebrad + zaino + zallet).
#
# Builds on the single-note spend covered by wallet_ironwood.py with the harder
# selection cases:
#   1. Multi-note combination: with two Ironwood notes present, a payment larger
#      than either note alone forces the wallet to combine both; the spend
#      consumes >= 2 Ironwood notes and the change stays Ironwood.
#   2. Drain: the change note from (1) can itself be spent, and a payment of the
#      whole remaining spendable Ironwood balance leaves only Ironwood change.
#   3. Confirmation gating: a freshly-confirmed Ironwood note is not spendable at
#      a higher minconf, and becomes spendable once it has enough confirmations.
#
# All spends are self-sends within a single account: viewing keys for an account
# created after the wallet's block scanner started are not picked up by the
# scanner, so a second account's received notes would never become spendable
# (a general, non-Ironwood wallet limitation; see wallet_ironwood.py).
#

from decimal import Decimal

from test_framework.util import (
    COIN,
    Pool,
    account_spendable_zat,
    assert_equal,
    assert_true,
    ironwood_notes,
    self_send,
    wait_for_account_spendable,
)
from test_framework.util_ironwood import (
    IronwoodTestFramework,
    ironwood_spends,
    shield_coinbase_into_ironwood,
)


class WalletIronwoodSpendingTest(IronwoodTestFramework):

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']

        # ---- Create two independent Ironwood notes ----------------------
        # Each shield sweeps the currently-mature coinbase into one Ironwood
        # note, so two shields (mining fresh coinbase between them) leave the
        # account holding two distinct Ironwood notes.
        print("Creating two Ironwood notes (two coinbase shields)...")
        txid_a, _ = shield_coinbase_into_ironwood(node, w, taddr, ua, acct)
        txid_b, _ = shield_coinbase_into_ironwood(node, w, taddr, ua, acct)
        assert_true(txid_a != txid_b, "Expected two distinct shield txs")

        # The shields' preflight `shieldingValue` can disagree with the value
        # actually swept across the regtest subsidy halving, so take the note
        # values from z_listunspent as ground truth and wait for the spendable
        # view to converge to their sum (the spendable balance lags a fresh
        # note; z_listunspent surfaces it first).
        notes = ironwood_notes(w)
        assert_true(len(notes) >= 2,
                    "Expected at least two Ironwood notes; got {}".format(notes))
        total = sum(int(n['valueZat']) for n in notes)
        largest_note_zat = max(int(n['valueZat']) for n in notes)
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.IRONWOOD, min_zat=total),
            total)
        print("  {} Ironwood notes, total {} zat, largest {} zat".format(
            len(notes), total, largest_note_zat))

        # ---- Test 1: a payment larger than any single note combines notes
        print("Test 1: a payment exceeding the largest note combines >= 2 notes...")
        # 1 ZEC above the largest single note: no single note can cover it, so
        # the wallet must draw on at least two Ironwood notes.
        send_zat = largest_note_zat + COIN
        assert_true(send_zat < total,
                    "Combined balance must exceed the single-note payment")
        send_zec = Decimal(send_zat) / COIN

        spend_txid, spend_fee = self_send(node, w, ua, send_zec)
        view = w.z_viewtransaction(spend_txid)
        spends = ironwood_spends(view)
        assert_true(
            len(spends) >= 2,
            "payment above the largest note must combine >= 2 Ironwood notes; "
            "got {} spends".format(len(spends)))
        spent_from = {s['txidPrev'] for s in spends}
        assert_true(
            {txid_a, txid_b}.issubset(spent_from),
            "both shield notes should be spent; spent from {}".format(spent_from))

        # Value stays Ironwood: change returns to the account, still Ironwood.
        expected_after = total - int(spend_fee * COIN)
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.IRONWOOD,
                                       min_zat=expected_after),
            expected_after)
        pools = w.z_getbalanceforaccount(acct)['pools']
        assert_true('orchard' not in pools,
                    "change must be Ironwood, not Orchard; got {}".format(pools))
        print("  Combined {} notes (fee {}); balance now {} zat. PASSED".format(
            len(spends), spend_fee, expected_after))

        # ---- Test 2: re-spend the change with a large multi-note send ---
        # The Ironwood change note from Test 1 must itself be spendable. Send a
        # large chunk back to the account (a self-send, so the whole balance
        # returns as new Ironwood notes minus the fee) and confirm the residual
        # is Ironwood only.
        print("Test 2: re-spending Ironwood change with a large self-send...")
        spendable = account_spendable_zat(w, acct, Pool.IRONWOOD)
        big_zec = (Decimal(spendable) / COIN) - Decimal('1')
        drain_txid, drain_fee = self_send(node, w, ua, big_zec)
        drain_view = w.z_viewtransaction(drain_txid)
        assert_true(len(ironwood_spends(drain_view)) >= 1,
                    "the change note should be spendable as an Ironwood input")
        # A self-send returns all value to the account, so the residual is the
        # prior spendable balance minus the fee, all Ironwood.
        expected_residual = spendable - int(drain_fee * COIN)
        residual = wait_for_account_spendable(
            w, acct, Pool.IRONWOOD, min_zat=expected_residual)
        assert_equal(residual, expected_residual)
        assert_true('orchard' not in w.z_getbalanceforaccount(acct)['pools'],
                    "residual must be Ironwood only")
        print("  Residual {} zat, Ironwood only (fee {}). PASSED"
              .format(residual, drain_fee))

        # ---- Test 3: confirmation gating --------------------------------
        print("Test 3: a freshly-confirmed note is gated by minconf...")
        # The change note from Test 2 has 1 confirmation. At minconf=10 it is not
        # yet spendable; after 9 more blocks it is.
        assert_equal(
            account_spendable_zat(w, acct, Pool.IRONWOOD, minconf=10), 0,
            "note with 1 confirmation must not be spendable at minconf=10")
        node.generate(9)
        self.sync_all()
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.IRONWOOD,
                                       min_zat=residual, minconf=10),
            residual)
        print("  Note became spendable at minconf=10 after 10 confirmations. "
              "PASSED")

        print("\nAll Ironwood spending tests passed!")


if __name__ == '__main__':
    WalletIronwoodSpendingTest().main()
