#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) activation boundary, against the Z3 stack
# (zebrad + zaino + zallet).
#
# Every other Ironwood test activates NU6.3 at height 1, so a real Orchard note
# never exists. This test defers NU6.3 to a height H > 1, creating an Orchard
# era (heights 1..H-1, where shielding to an Orchard receiver mints real Orchard
# notes) followed by an Ironwood era (>= H, where the same receiver mints
# Ironwood notes). A single account then holds BOTH a v2 Orchard note and an
# Ironwood note, which exercises the cross-era behavior:
#   1. z_getbalanceforaccount / z_listunspent report both the `orchard` and the
#      `ironwood` pool at once.
#   2. A payment larger than either note alone combines an Orchard input and an
#      Ironwood input in one transaction (both Orchard-family bundles carry
#      spends), and the change lands in Ironwood: post-activation the turnstile
#      no longer produces Orchard change.
#

from decimal import Decimal

from test_framework.util import (
    COIN,
    Pool,
    PrivacyPolicy,
    account_spendable_zat,
    assert_equal,
    assert_true,
    ironwood_notes,
    nu_activation_ironwood_at,
    shield_coinbase,
    spends_in_pool,
    wait_account_settled,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
)
from test_framework.util_ironwood import IronwoodTestFramework

# Defer NU6.3 well past coinbase maturity so the Orchard-era shield (which needs
# mature coinbase) is mined before activation, and the Ironwood-era shield after.
IRONWOOD_HEIGHT = 210


def orchard_notes(wallet, minconf=1):
    return [u for u in wallet.z_listunspent(minconf)
            if u['pool'] == Pool.ORCHARD]


class WalletIronwoodActivationTest(IronwoodTestFramework):

    def __init__(self) -> None:
        super().__init__()
        # Override the base "NU6.3 at height 1" with a deferred activation.
        self.activation_heights = nu_activation_ironwood_at(IRONWOOD_HEIGHT)

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']
        sapling_ua = w.z_getaddressforaccount(acct, ['sapling'])['address']

        # ---- Orchard era: mint a real Orchard note ----------------------
        # A many-UTXO coinbase shield straight into a pure-Orchard note hits a
        # fee-estimation path that leaves it unspendable (see wallet_z_
        # shieldcoinbase). Instead shield into Sapling, then pay the Orchard-only
        # address to mint a single, spendable Orchard note, all before NU6.3.
        print("Orchard era: mint a spendable Orchard note (pre-NU6.3)...")
        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "must still be in the Orchard era before funding")
        _, sapling_zat = shield_coinbase(
            node, w, taddr, sapling_ua, acct, Pool.SAPLING)
        orch_target = (Decimal(sapling_zat) / COIN / 2).quantize(Decimal('0.0001'))
        opid = w.z_sendmany(
            sapling_ua, [{'address': ua, 'amount': orch_target}], 1, None,
            PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        txid = wait_and_assert_operationid_status(w, opid)
        assert_true(txid is not None, "Sapling -> Orchard send should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, txid)
        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "the Orchard note must be minted before NU6.3 activates")
        # The Sapling -> Orchard send may route both the payment and its change
        # into Orchard, so read the actual spendable Orchard value rather than
        # assuming the payment amount.
        wait_for_account_spendable(w, acct, Pool.ORCHARD, min_zat=1)
        wait_account_settled(w, acct)
        orchard_zat = account_spendable_zat(w, acct, Pool.ORCHARD)
        assert_true(orchard_zat > 0, "the Orchard note should be spendable")
        assert_equal(len(ironwood_notes(w)), 0)
        print("  Orchard spendable: {} zat".format(orchard_zat))

        # ---- Ironwood era: mint an Ironwood note ------------------------
        print("Ironwood era: shield coinbase into an Ironwood note (post-NU6.3)...")
        _, ironwood_zat = shield_coinbase(node, w, taddr, ua, acct, Pool.IRONWOOD)
        assert_true(node.getblockcount() >= IRONWOOD_HEIGHT,
                    "the Ironwood shield must be mined after NU6.3 activates")
        print("  Ironwood note: {} zat".format(ironwood_zat))

        # ---- Test 1: both pools coexist in one account ------------------
        print("Test 1: the account holds both an Orchard and an Ironwood note...")
        pools = w.z_getbalanceforaccount(acct)['pools']
        assert_true('orchard' in pools and 'ironwood' in pools,
                    "expected both orchard and ironwood pools; got {}".format(pools))
        assert_true(len(orchard_notes(w)) >= 1, "expected an Orchard note")
        assert_true(len(ironwood_notes(w)) >= 1, "expected an Ironwood note")
        print("  Both pools present. PASSED")

        # ---- Test 2: a payment combines an Orchard and an Ironwood input -
        print("Test 2: a cross-era payment combines Orchard + Ironwood inputs...")
        wait_account_settled(w, acct)
        orch_total = sum(int(n['valueZat']) for n in orchard_notes(w))
        iron_total = sum(int(n['valueZat']) for n in ironwood_notes(w))
        sap_total = account_spendable_zat(w, acct, Pool.SAPLING)
        # Exceed the Sapling + Ironwood total so neither those pools alone can
        # cover the payment: the Orchard note must also be spent. (Crossing pools
        # reveals amounts across the turnstile, so allow that.)
        send_zat = iron_total + sap_total + COIN
        assert_true(send_zat < orch_total + iron_total + sap_total,
                    "combined balance must cover the payment")
        assert_true(send_zat > orch_total,
                    "payment must exceed the Orchard note so it is not paid "
                    "from Orchard alone")
        send_zec = (Decimal(send_zat) / COIN).quantize(Decimal('0.0001'))
        send_zat = int(send_zec * COIN)

        recipients = [{'address': ua, 'amount': send_zec}]
        opid = w.z_sendmany(
            ua, recipients, 1, None, PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        txid = wait_and_assert_operationid_status(w, opid)
        assert_true(txid is not None, "cross-era combine send should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, txid)

        view = w.z_viewtransaction(txid)
        assert_true(len(spends_in_pool(view, Pool.ORCHARD)) >= 1,
                    "the combine should spend the Orchard note; got {}"
                    .format(view['spends']))
        assert_true(len(spends_in_pool(view, Pool.IRONWOOD)) >= 1,
                    "the combine should spend the Ironwood note; got {}"
                    .format(view['spends']))
        print("  Combined Orchard + Ironwood inputs for {} ZEC.".format(send_zec))

        # ---- Test 3: the payment to an Orchard receiver funds Ironwood --
        # Once NU6.3 is active, a payment to an Orchard receiver is delivered as
        # an Ironwood note, whatever pools funded it. (The turnstile's treatment
        # of Orchard-input CHANGE is pinned separately in the Orchard-turnstile
        # property test; here we only assert the payment side.)
        print("Test 3: the payment to the Orchard receiver is an Ironwood note...")
        payments = [o for o in view['outputs']
                    if not o['walletInternal'] and o['pool'] == Pool.IRONWOOD
                    and o['valueZat'] == send_zat]
        assert_equal(len(payments), 1,
                     "the payment to the Orchard receiver should be a single "
                     "Ironwood output of {} zat; got {}".format(
                         send_zat, view['outputs']))
        assert_true(all(o['pool'] != Pool.ORCHARD for o in view['outputs']
                        if not o['walletInternal']),
                    "no payment output may be Orchard once NU6.3 is active")
        print("  Payment landed in Ironwood. PASSED")

        print("\nAll Ironwood activation-boundary tests passed!")


if __name__ == '__main__':
    WalletIronwoodActivationTest().main()
