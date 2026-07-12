#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) Orchard turnstile properties, against the Z3 stack
# (zebrad + zaino + zallet).
#
# Once NU6.3 is active, the Orchard pool is wound down through a one-way
# turnstile. This test pins the exact rules:
#   P1. It is impossible to send funds INTO the Orchard pool: a payment to an
#       Orchard receiver is always delivered as an Ironwood note, never Orchard.
#   P2. It is possible to send funds FROM Orchard to Ironwood (spending an
#       Orchard note to pay an Orchard receiver routes the payment to Ironwood).
#   P3a. When spending FROM Orchard to another pool, any change MUST return to
#        the Orchard pool.
#   P3b. Paying an Orchard receiver funds Ironwood no matter which pool the
#        inputs came from (transparent, Sapling, Orchard, or Ironwood).
#
# NU6.3 is deferred to a height so a spendable Orchard note can be minted in the
# Orchard era first. A shielded send (AllowRevealedAmounts) never draws
# transparent inputs, so a specific SHIELDED input pool is forced by paying more
# than the other shielded pools hold; a transparent input is forced by naming a
# t-addr as the `fromaddress`.
#

import re
from decimal import Decimal, ROUND_DOWN

from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    AuthJSONRPCException,
    Pool,
    PrivacyPolicy,
    account_spendable_zat,
    assert_true,
    outputs_in_pool,
    shield_coinbase,
    spends_in_pool,
    wait_account_settled,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
    wait_for_wallet_sync,
)
from test_framework.util_ironwood import IronwoodTestFramework
from test_framework.util import nu_activation_ironwood_at

IRONWOOD_HEIGHT = 210

# "Failed to propose transaction: Insufficient balance (have 123, need 456
# including fee)" -- the fee for combining many small notes (e.g. a pool that
# accumulated several separate coinbase/change notes over the course of the
# test) can exceed force_shielded_amount()'s fixed safety buffer. Rather than
# guess a buffer large enough for every possible note layout, extract the
# exact shortfall from the proposal error and retry with the amount reduced
# by it.
_INSUFFICIENT_BALANCE_RE = re.compile(
    r"Insufficient balance \(have (\d+), need (\d+) including fee\)")


def payment_outputs(view, pool):
    """The non-change outputs of a tx view that are in `pool`."""
    return [o for o in outputs_in_pool(view, pool) if not o['walletInternal']]


def change_outputs(view, pool):
    """The change (wallet-internal) outputs of a tx view that are in `pool`."""
    return [o for o in outputs_in_pool(view, pool) if o['walletInternal']]


class WalletIronwoodOrchardTurnstileTest(IronwoodTestFramework):

    def __init__(self) -> None:
        super().__init__()
        self.activation_heights = nu_activation_ironwood_at(IRONWOOD_HEIGHT)

    def other_shielded(self, w, acct, exclude: Pool) -> int:
        """Spendable value in the shielded pools other than `exclude`, in
        zatoshis. Transparent is omitted because a shielded (AllowRevealed
        Amounts) send never draws transparent inputs."""
        return sum(account_spendable_zat(w, acct, p)
                   for p in (Pool.SAPLING, Pool.ORCHARD, Pool.IRONWOOD)
                   if p != exclude)

    # Comfortably covers a multi-action ZIP-317 fee, so the forced amount plus
    # its fee still fits inside the combined spendable balance.
    FEE_BUFFER_ZAT = 100000  # 0.001 ZEC

    def force_shielded_amount(self, w, acct, pool: Pool) -> Decimal:
        """A ZEC amount that can only be covered by also spending `pool`: one ZEC
        more than every other shielded pool holds, minus a fee buffer (a bare
        `other + 1 ZEC` ceiling leaves no room for the send's own fee, and
        rounding the resulting Decimal could round up past the coverable
        amount)."""
        other = self.other_shielded(w, acct, pool)
        amount_zat = other + COIN - self.FEE_BUFFER_ZAT
        assert_true(other < amount_zat <= account_spendable_zat(w, acct, pool) + other,
                    "forced amount must exceed {} alone and be coverable "
                    "together with it".format(pool))
        return (Decimal(amount_zat) / COIN).quantize(
            Decimal('0.0001'), rounding=ROUND_DOWN)

    def send(self, node, w, from_addr, to_addr, amount, policy):
        """z_sendmany one recipient, confirm, settle, and return the tx view.

        Retries with the amount reduced by the exact reported shortfall if
        the wallet's proposal fails on insufficient balance -- see
        _INSUFFICIENT_BALANCE_RE above."""
        opid = None
        for attempt in range(5):
            try:
                opid = w.z_sendmany(
                    from_addr, [{'address': to_addr, 'amount': amount}], 1,
                    None, policy)
                break
            except AuthJSONRPCException as e:
                match = _INSUFFICIENT_BALANCE_RE.search(
                    e.error.get('message', ''))
                if not match or attempt == 4:
                    raise
                have, need = int(match.group(1)), int(match.group(2))
                amount = (amount - Decimal(need - have) / COIN).quantize(
                    Decimal('0.0001'), rounding=ROUND_DOWN)
        txid = wait_and_assert_operationid_status(w, opid)
        assert_true(txid is not None, "send should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, txid)
        return w.z_viewtransaction(txid)

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']
        sapling_ua = w.z_getaddressforaccount(acct, ['sapling'])['address']

        # ---- Orchard era: mint a spendable Orchard note -----------------
        # (Shield into Sapling then pay the Orchard-only address; a many-UTXO
        # pure-Orchard coinbase shield leaves the note unspendable.)
        print("Orchard era: mint a spendable Orchard note (pre-NU6.3)...")
        _, sap_zat = shield_coinbase(node, w, taddr, sapling_ua, acct, Pool.SAPLING)
        orch_target = (Decimal(sap_zat) / COIN * Decimal('0.9')).quantize(
            Decimal('0.0001'))
        self.send(node, w, sapling_ua, ua, orch_target,
                  PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "the Orchard note must be minted before NU6.3")
        wait_for_account_spendable(w, acct, Pool.ORCHARD, min_zat=1)
        wait_account_settled(w, acct)
        assert_true(account_spendable_zat(w, acct, Pool.ORCHARD) > 0,
                    "need a spendable Orchard note")

        # Cross into the Ironwood era (a few blocks past activation), then wait
        # for the WALLET to see the new tip so it builds spends with the NU6.3
        # consensus branch id (otherwise the node rejects them). The account's
        # only shielded value is now the Orchard note.
        target = IRONWOOD_HEIGHT + 10
        if node.getblockcount() < target:
            node.generate(target - node.getblockcount())
        wait_for_wallet_sync(node, w)
        wait_account_settled(w, acct)
        assert_true(node.getblockcount() >= IRONWOOD_HEIGHT, "NU6.3 must be active")
        print("  NU6.3 active at height {}".format(node.getblockcount()))

        # ---- P2 + P1 + P3a: Orchard -> Orchard receiver -----------------
        # Orchard is the only shielded pool with meaningful value, so a shielded
        # send draws it. The payment to the Orchard receiver must be Ironwood,
        # and the change must return to Orchard.
        print("P2/P1/P3a: spending Orchard to an Orchard receiver...")
        amount = self.force_shielded_amount(w, acct, Pool.ORCHARD)
        view = self.send(node, w, ua, ua, amount,
                         PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        assert_true(len(spends_in_pool(view, Pool.ORCHARD)) >= 1,
                    "the Orchard note must be spent; got {}".format(view['spends']))
        assert_true(len(payment_outputs(view, Pool.IRONWOOD)) >= 1,
                    "payment to an Orchard receiver must be Ironwood; got {}"
                    .format(view['outputs']))
        assert_true(len(payment_outputs(view, Pool.ORCHARD)) == 0,
                    "no Orchard PAYMENT may be created post-NU6.3; got {}"
                    .format(view['outputs']))
        assert_true(len(change_outputs(view, Pool.ORCHARD)) >= 1,
                    "Orchard-input change must return to Orchard; got {}"
                    .format(view['outputs']))
        print("  Payment -> Ironwood, change -> Orchard. PASSED")

        # ---- P3a: Orchard -> Sapling receiver, change stays Orchard -----
        # Force Orchard as an input (pay more than the other shielded pools) and
        # send to a Sapling receiver; the Orchard-input change stays Orchard.
        print("P3a: spending Orchard to a Sapling receiver...")
        wait_account_settled(w, acct)
        amount = self.force_shielded_amount(w, acct, Pool.ORCHARD)
        view = self.send(node, w, ua, sapling_ua, amount,
                         PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        assert_true(len(spends_in_pool(view, Pool.ORCHARD)) >= 1,
                    "the Orchard note must be spent")
        assert_true(len(payment_outputs(view, Pool.SAPLING)) >= 1,
                    "payment to a Sapling receiver must be Sapling")
        assert_true(len(change_outputs(view, Pool.ORCHARD)) >= 1,
                    "Orchard-input change must stay Orchard; got {}"
                    .format(view['outputs']))
        print("  Change stayed Orchard. PASSED")

        # ---- P3b: paying an Orchard receiver funds Ironwood from any pool -
        print("P3b: paying an Orchard receiver funds Ironwood from each pool...")

        # From transparent (coinbase): z_sendmany does not select coinbase
        # UTXOs, so use z_shieldcoinbase, which spends transparent coinbase into
        # the Orchard receiver. Post-NU6.3 the shielded note must be Ironwood.
        node.generate(COINBASE_MATURITY + 5)
        wait_for_wallet_sync(node, w)
        result = w.z_shieldcoinbase(taddr, ua)
        txid = wait_and_assert_operationid_status(w, result['opid'])
        assert_true(txid is not None, "shielding coinbase should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, txid)
        view = w.z_viewtransaction(txid)
        assert_true(len(spends_in_pool(view, Pool.TRANSPARENT)) >= 1,
                    "expected transparent (coinbase) inputs; got {}"
                    .format(view['spends']))
        assert_true(len(payment_outputs(view, Pool.IRONWOOD)) >= 1,
                    "transparent -> Orchard receiver must fund Ironwood; got {}"
                    .format(view['outputs']))
        print("  transparent -> Ironwood. PASSED")

        # From Ironwood: force an Ironwood input (pay more than the other
        # shielded pools hold).
        wait_account_settled(w, acct)
        amount = self.force_shielded_amount(w, acct, Pool.IRONWOOD)
        view = self.send(node, w, ua, ua, amount,
                         PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        assert_true(len(spends_in_pool(view, Pool.IRONWOOD)) >= 1,
                    "expected an Ironwood input; got {}".format(view['spends']))
        assert_true(len(payment_outputs(view, Pool.IRONWOOD)) >= 1,
                    "Ironwood -> Orchard receiver must fund Ironwood")
        print("  Ironwood -> Ironwood. PASSED")

        print("\nAll Ironwood Orchard-turnstile tests passed!")


if __name__ == '__main__':
    WalletIronwoodOrchardTurnstileTest().main()
