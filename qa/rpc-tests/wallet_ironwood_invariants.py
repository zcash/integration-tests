#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) invariant / model-based scenario, against the Z3
# stack (zebrad + zaino + zallet).
#
# Rather than assert values from one scripted path, this drives a deterministic
# sequence of shielded payments (self-sends and turnstile crossings) and, after
# EACH step, checks three invariants that must hold regardless of the path the
# builder took:
#   1. Conservation: the chain's total shielded value drops by exactly the fee
#      of the step (value never appears or vanishes; only fees leave).
#   2. Pool exclusivity: once NU6.3 is active the account never holds an Orchard
#      note (Orchard-family value is always Ironwood).
#   3. Reconciliation: the wallet's total shielded balance equals the node's
#      total shielded value (consensus and wallet never diverge).
#
# This catches the class of bug that only shows up after a specific interleaving
# of operations, which no single scripted assertion reaches.
#

from decimal import Decimal

from test_framework.util import (
    COIN,
    Pool,
    PrivacyPolicy,
    account_balance_zat,
    assert_equal,
    assert_true,
    shield_coinbase,
    wait_account_settled,
    wait_and_assert_operationid_status,
    wait_for_tx_scanned,
)
from test_framework.util_ironwood import IronwoodTestFramework

SHIELDED_POOLS = ('sapling', 'orchard', 'ironwood')


def total_shielded_zat(node) -> int:
    """The chain's total shielded value across Sapling, Orchard, and Ironwood."""
    total = 0
    for p in node.getblockchaininfo()['valuePools']:
        if p['id'] in SHIELDED_POOLS:
            total += int(p['chainValueZat'])
    return total


class WalletIronwoodInvariantsTest(IronwoodTestFramework):

    def check_invariants(self, node, w, acct, total_before: int,
                         fee_zat: int) -> int:
        """Assert the three invariants after a mined step and return the new
        chain total shielded value."""
        # 1. Conservation: total shielded fell by exactly the fee.
        total_after = total_shielded_zat(node)
        assert_equal(total_before - total_after, fee_zat,
                     "total shielded value must fall by exactly the fee")

        # 2. Pool exclusivity: no Orchard note once NU6.3 is active.
        pools = w.z_getbalanceforaccount(acct)['pools']
        assert_true('orchard' not in pools,
                    "post-NU6.3 the account must hold no Orchard note; got {}"
                    .format(pools))

        # 3. Reconciliation: wallet total shielded == node total shielded.
        # account_balance_zat counts pending notes, so it matches the chain as
        # soon as the confirming block is scanned.
        wallet_total = (account_balance_zat(w, acct, Pool.IRONWOOD)
                        + account_balance_zat(w, acct, Pool.SAPLING))
        assert_equal(wallet_total, total_after,
                     "wallet total shielded must equal the chain's")
        return total_after

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']
        sapling_ua = w.z_getaddressforaccount(acct, ['sapling'])['address']

        print("Seeding the account with an Ironwood note...")
        shield_coinbase(node, w, taddr, ua, acct, Pool.IRONWOOD)
        total = total_shielded_zat(node)
        # The seed itself must reconcile.
        assert_equal(account_balance_zat(w, acct, Pool.IRONWOOD), total)

        # A fixed sequence exercising same-pool sends and turnstile crossings in
        # both directions (to an Orchard receiver -> Ironwood, to a Sapling
        # receiver -> Sapling). Amounts are small enough to always be coverable.
        steps = [
            (ua, Decimal('1')),          # Ironwood -> Ironwood
            (sapling_ua, Decimal('2')),  # cross into Sapling
            (ua, Decimal('1')),          # back toward Ironwood
            (sapling_ua, Decimal('1')),  # cross into Sapling again
            (ua, Decimal('3')),          # Ironwood-heavy send
        ]

        for i, (dest, amount) in enumerate(steps, start=1):
            print("Step {}: send {} ZEC to {}...".format(
                i, amount, 'sapling' if dest == sapling_ua else 'orchard'))
            opid = w.z_sendmany(
                ua, [{'address': dest, 'amount': amount}], 1, None,
                PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
            txid = wait_and_assert_operationid_status(w, opid)
            assert_true(txid is not None, "step {} send should succeed".format(i))
            node.generate(1)
            details = wait_for_tx_scanned(w, txid)
            fee_zat = int(Decimal(details['fee']) * COIN)
            total = self.check_invariants(node, w, acct, total, fee_zat)
            # Let the change settle so the next step has spendable funds.
            wait_account_settled(w, acct)
            print("  invariants hold (fee {} zat, total shielded {} zat)".format(
                fee_zat, total))

        print("\nAll Ironwood invariant checks passed!")


if __name__ == '__main__':
    WalletIronwoodInvariantsTest().main()
