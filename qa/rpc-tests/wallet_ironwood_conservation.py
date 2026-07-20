#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) consensus value-pool accounting, against the Z3
# stack (zebrad + zaino + zallet).
#
# The other Ironwood tests assert the wallet's balance. This one asserts the
# NODE's value-pool accounting (zebra's `getblockchaininfo` reports an
# `ironwood` entry in `valuePools`) and reconciles it with the wallet, checking
# the invariant that matters for a value pool: shielded value is conserved
# except for fees, which leave the shielded pools to the miner.
#   1. Shielding X into Ironwood raises the chain's Ironwood pool by exactly X,
#      matching the wallet's Ironwood balance.
#   2. A self-send lowers the Ironwood pool by exactly the fee (the value stays
#      in Ironwood as change; only the fee leaves).
#   3. Crossing A from Ironwood to Sapling raises the Sapling pool by A and
#      lowers Ironwood by A + fee, so the total shielded value drops by exactly
#      the fee.
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
    self_send,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
)
from test_framework.util_ironwood import IronwoodTestFramework

SHIELDED_POOLS = ('sapling', 'orchard', 'ironwood')


def pool_chain_value_zat(node, pool_id: str) -> int:
    """The chain's cumulative value in `pool_id`, in zatoshis, from the node's
    getblockchaininfo valuePools (consensus accounting, not the wallet)."""
    for p in node.getblockchaininfo()['valuePools']:
        if p['id'] == pool_id:
            return int(p['chainValueZat'])
    raise AssertionError("no {} pool in valuePools".format(pool_id))


def total_shielded_zat(node) -> int:
    """The chain's total shielded value across Sapling, Orchard, and Ironwood."""
    return sum(pool_chain_value_zat(node, p) for p in SHIELDED_POOLS)


class WalletIronwoodConservationTest(IronwoodTestFramework):

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']
        sapling_ua = w.z_getaddressforaccount(acct, ['sapling'])['address']

        assert_equal(pool_chain_value_zat(node, 'ironwood'), 0)

        # ---- Test 1: shielding raises the chain Ironwood pool by X ------
        print("Test 1: the chain Ironwood pool matches the shielded value...")
        _, ironwood_zat = shield_coinbase(node, w, taddr, ua, acct, Pool.IRONWOOD)
        assert_equal(pool_chain_value_zat(node, 'ironwood'), ironwood_zat,
                     "chain Ironwood pool should equal the shielded note value")
        # The node and the wallet agree (the wallet owns all Ironwood value).
        assert_equal(account_balance_zat(w, acct, Pool.IRONWOOD), ironwood_zat)
        print("  Chain Ironwood pool = {} zat, matches wallet. PASSED"
              .format(ironwood_zat))

        # ---- Test 2: a self-send removes only the fee ------------------
        print("Test 2: a self-send lowers the Ironwood pool by exactly the fee...")
        before = pool_chain_value_zat(node, 'ironwood')
        total_before = total_shielded_zat(node)
        _, fee = self_send(node, w, ua, Decimal('1'))
        fee_zat = int(fee * COIN)
        after = pool_chain_value_zat(node, 'ironwood')
        assert_equal(before - after, fee_zat,
                     "the Ironwood pool should drop by exactly the fee")
        assert_equal(total_shielded_zat(node) - total_before, -fee_zat,
                     "total shielded value should drop by exactly the fee")
        # Wait for the change to settle, then reconcile node and wallet.
        wait_for_account_spendable(w, acct, Pool.IRONWOOD, min_zat=after)
        assert_equal(account_balance_zat(w, acct, Pool.IRONWOOD), after)
        print("  Ironwood pool dropped by the fee {} zat. PASSED".format(fee_zat))

        # ---- Test 3: a turnstile crossing conserves value minus fee ----
        print("Test 3: crossing Ironwood -> Sapling conserves value minus fee...")
        iron_before = pool_chain_value_zat(node, 'ironwood')
        sap_before = pool_chain_value_zat(node, 'sapling')
        total_before = total_shielded_zat(node)

        cross = Decimal('5')
        cross_zat = int(cross * COIN)
        opid = w.z_sendmany(
            ua, [{'address': sapling_ua, 'amount': cross}], 1, None,
            PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        txid = wait_and_assert_operationid_status(w, opid)
        assert_true(txid is not None, "cross-pool send should succeed")
        node.generate(1)
        details = wait_for_tx_scanned(w, txid)
        fee_zat = int(Decimal(details['fee']) * COIN)

        iron_after = pool_chain_value_zat(node, 'ironwood')
        sap_after = pool_chain_value_zat(node, 'sapling')
        assert_equal(sap_after - sap_before, cross_zat,
                     "the Sapling pool should rise by the crossed amount")
        assert_equal(iron_before - iron_after, cross_zat + fee_zat,
                     "the Ironwood pool should fall by the crossed amount + fee")
        assert_equal(total_shielded_zat(node) - total_before, -fee_zat,
                     "total shielded value should fall by exactly the fee")
        # Reconcile with the wallet's per-pool balances.
        wait_for_account_spendable(w, acct, Pool.SAPLING, min_zat=sap_after)
        assert_equal(account_balance_zat(w, acct, Pool.SAPLING), sap_after)
        assert_equal(account_balance_zat(w, acct, Pool.IRONWOOD), iron_after)
        print("  Sapling +{}, Ironwood -{} (fee {}); total conserved. PASSED"
              .format(cross_zat, cross_zat + fee_zat, fee_zat))

        print("\nAll Ironwood conservation tests passed!")


if __name__ == '__main__':
    WalletIronwoodConservationTest().main()
