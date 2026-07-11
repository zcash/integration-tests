#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) cross-pool spends, against the Z3 stack
# (zebrad + zaino + zallet).
#
# A z_sendmany `fromaddress` resolves to an account, and the default spend
# policy permits drawing from every shielded pool the account holds. This test
# exercises Ironwood interacting with Sapling across the turnstile:
#   1. Turnstile crossing Ironwood -> Sapling: with only Ironwood funds, paying
#      a Sapling receiver moves value across pools; the recipient note is
#      Sapling and becomes spendable there.
#   2. Sapling + Ironwood combined inputs: a payment larger than either pool's
#      total forces the wallet to draw from both a Sapling note and an Ironwood
#      note in one transaction.
#
# Cross-pool sends reveal amounts across the turnstile, so they are issued with
# the `AllowRevealedAmounts` privacy policy. All sends stay within one account
# (see wallet_ironwood.py for why a second account cannot become spendable).
#

from decimal import Decimal

from test_framework.util import (
    COIN,
    Pool,
    PrivacyPolicy,
    RpcProxy,
    account_spendable_zat,
    assert_equal,
    assert_true,
    ironwood_notes,
    outputs_in_pool,
    shield_coinbase,
    spends_in_pool,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
)
from test_framework.util_ironwood import IronwoodTestFramework


class WalletIronwoodCrossPoolTest(IronwoodTestFramework):

    def cross_pool_send(self, node: RpcProxy, w: RpcProxy, from_ua: str,
                        to_addr: str, amount: Decimal) -> dict:
        """z_sendmany `amount` ZEC from the account behind `from_ua` to
        `to_addr`, allowing revealed amounts (required to cross the turnstile),
        confirm it, and return the z_viewtransaction view."""
        recipients = [{'address': to_addr, 'amount': amount}]
        opid = w.z_sendmany(
            from_ua, recipients, 1, None, PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        txid = wait_and_assert_operationid_status(w, opid)
        assert_true(txid is not None, "cross-pool send should have succeeded")
        node.generate(1)
        wait_for_tx_scanned(w, txid)
        return w.z_viewtransaction(txid)

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        orchard_ua = w.z_getaddressforaccount(acct, ['orchard'])['address']
        sapling_ua = w.z_getaddressforaccount(acct, ['sapling'])['address']

        # ---- Test 1: turnstile crossing Ironwood -> Sapling -------------
        print("Test 1: paying a Sapling receiver from Ironwood funds...")
        _, ironwood_zat = shield_coinbase(
            node, w, taddr, orchard_ua, acct, Pool.IRONWOOD)
        assert_equal(account_spendable_zat(w, acct, Pool.SAPLING), 0)

        cross_zec = (Decimal(ironwood_zat) / COIN / 2).quantize(Decimal('0.0001'))
        view = self.cross_pool_send(node, w, orchard_ua, sapling_ua, cross_zec)

        # The spend consumed Ironwood; the payment landed in Sapling.
        assert_true(len(spends_in_pool(view, Pool.IRONWOOD)) >= 1,
                    "cross-pool send should spend an Ironwood note")
        sap_out = outputs_in_pool(view, Pool.SAPLING)
        assert_true(len(sap_out) >= 1,
                    "payment to a Sapling receiver should create a Sapling "
                    "output; got {}".format(view['outputs']))
        cross_zat = int(cross_zec * COIN)
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.SAPLING, min_zat=cross_zat),
            cross_zat)
        print("  Ironwood -> Sapling crossing of {} ZEC. PASSED".format(cross_zec))

        # ---- Test 2: Sapling + Ironwood combined inputs -----------------
        print("Test 2: a payment forcing Sapling + Ironwood combination...")
        # Top up Sapling so both pools hold spendable value.
        shield_coinbase(node, w, taddr, sapling_ua, acct, Pool.SAPLING)

        # Derive each pool's total from its confirmed notes, then wait for the
        # spendable view to converge to those totals (the spendable balance lags
        # a fresh note; z_listunspent surfaces it first).
        total_iron = sum(int(n['valueZat']) for n in ironwood_notes(w))
        total_sap = sum(int(u['valueZat']) for u in w.z_listunspent(1)
                        if u['pool'] == Pool.SAPLING)
        assert_true(total_iron > 0 and total_sap > 0,
                    "both pools must hold spendable value (iron={}, sap={})"
                    .format(total_iron, total_sap))
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.IRONWOOD,
                                       min_zat=total_iron),
            total_iron)
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.SAPLING,
                                       min_zat=total_sap),
            total_sap)

        # Exceed the larger pool total by 1 ZEC: neither pool alone can cover the
        # payment, so both must contribute an input.
        combine_zat = max(total_iron, total_sap) + COIN
        assert_true(combine_zat < total_iron + total_sap,
                    "combined balance must cover the payment")
        combine_zec = (Decimal(combine_zat) / COIN).quantize(Decimal('0.0001'))

        view = self.cross_pool_send(node, w, orchard_ua, orchard_ua, combine_zec)
        assert_true(len(spends_in_pool(view, Pool.SAPLING)) >= 1,
                    "combined payment should spend a Sapling note; got {}"
                    .format(view['spends']))
        assert_true(len(spends_in_pool(view, Pool.IRONWOOD)) >= 1,
                    "combined payment should spend an Ironwood note; got {}"
                    .format(view['spends']))
        print("  Combined Sapling + Ironwood inputs for {} ZEC. PASSED"
              .format(combine_zec))

        print("\nAll Ironwood cross-pool tests passed!")


if __name__ == '__main__':
    WalletIronwoodCrossPoolTest().main()
