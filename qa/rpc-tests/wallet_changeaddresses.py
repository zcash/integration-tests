#!/usr/bin/env python3
# Copyright (c) 2019-2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Transparent change addresses are never reused.
#
# A fully-transparent send returns its change to an internal-scope (BIP 44)
# transparent address. Reusing one across transactions would publicly link them,
# so each send must reserve a fresh one.
#
# Migrated from the zcashd test to the Z3 stack (zebrad + zaino + zallet). Two
# behavioural differences from zcashd are pinned deliberately:
#
#   * zcashd could source a t-send from `ANY_TADDR`; zallet spends transparent
#     funds only when `fromaddress` names a single transparent address, and
#     draws only on that address's UTXOs. So `taddr_source` is funded with
#     several separate UTXOs, and each send consumes one of them.
#
#   * A t-to-Sapling send is NOT fully transparent (it has a shielded output),
#     so zallet shields its change rather than returning it to the transparent
#     pool as zcashd did. That case therefore asserts the absence of transparent
#     change instead of change-address rotation; only the t-to-t case can
#     exercise transparent change at all.
#

from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    Pool,
    PrivacyPolicy,
    assert_equal,
    assert_true,
    first_transparent_receiver,
    transparent_change_address,
    transparent_output_addresses,
    unified_address_for,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_stable_transparent,
    wait_for_tx_scanned,
)

# Each send from `taddr_source` consumes one of its UTXOs, and the change goes to
# a fresh internal address rather than back to the source. Four sends are made
# (two per policy under test), so fund the source with one UTXO each, plus slack.
NUM_SOURCE_UTXOS = 6
SOURCE_UTXO_VALUE = Decimal('2')
SEND_VALUE = Decimal('1')

DIVERSIFIER_SHIELDED = 10
DIVERSIFIER_SOURCE = 11
DIVERSIFIER_TARGET = 12


class WalletChangeAddressesTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        miner_taddr = self.miner_addresses[0]

        self.sync_all()
        acct = w.z_listaccounts()[0]['account_uuid']

        ua_shielded = unified_address_for(w, acct, DIVERSIFIER_SHIELDED)
        taddr_source = first_transparent_receiver(
            w, unified_address_for(w, acct, DIVERSIFIER_SOURCE))
        taddr_target = first_transparent_receiver(
            w, unified_address_for(w, acct, DIVERSIFIER_TARGET))

        # Shield coinbase: coinbase cannot be spent transparently, so this is the
        # only route to spendable non-coinbase transparent funds.
        node.generate(COINBASE_MATURITY + 20)
        wait_for_mature_coinbase_count(
            w, node.getblockcount() - COINBASE_MATURITY + 1)

        result = w.z_shieldcoinbase(miner_taddr, ua_shielded)
        shielding_value = Decimal(result['shieldingValue'])
        shield_txid = wait_and_assert_operationid_status(w, result['opid'])
        node.generate(1)
        shield_details = wait_for_tx_scanned(w, shield_txid)
        shielded_zec = shielding_value - Decimal(shield_details['fee'])
        wait_for_account_spendable(
            w, acct, Pool.ORCHARD, min_zat=int(shielded_zec * COIN))

        # Fund `taddr_source` with several distinct UTXOs.
        print('Funding %s with %d UTXOs of %s ZEC'
              % (taddr_source, NUM_SOURCE_UTXOS, SOURCE_UTXO_VALUE))
        for i in range(NUM_SOURCE_UTXOS):
            opid = w.z_sendmany(
                ua_shielded,
                [{'address': taddr_source, 'amount': SOURCE_UTXO_VALUE}],
                MIN_CONFIRMATIONS,
                INTERNAL_FEE,
                PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
            txid = wait_and_assert_operationid_status(w, opid)
            node.generate(1)
            wait_for_tx_scanned(w, txid)
            # Each send consumes the account's Orchard note and returns the
            # remainder as shielded change; that change must regain a commitment
            # tree position before the next iteration can spend it.
            if i + 1 < NUM_SOURCE_UTXOS:
                wait_for_account_spendable(
                    w, acct, Pool.ORCHARD,
                    min_zat=int((SOURCE_UTXO_VALUE + Decimal('0.01')) * COIN))
        wait_for_stable_transparent(w, min_count=NUM_SOURCE_UTXOS)

        source_utxos = [u for u in w.z_listunspent(MIN_CONFIRMATIONS)
                        if u['pool'] == Pool.TRANSPARENT
                        and u['address'] == taddr_source]
        assert_equal(len(source_utxos), NUM_SOURCE_UTXOS)

        def send_twice(target, policy):
            """Send to `target` twice from `taddr_source`, returning both txids."""
            txids = []
            for _ in range(2):
                opid = w.z_sendmany(
                    taddr_source,
                    [{'address': target, 'amount': SEND_VALUE}],
                    MIN_CONFIRMATIONS,
                    INTERNAL_FEE,
                    policy)
                txid = wait_and_assert_operationid_status(w, opid)
                node.generate(1)
                wait_for_tx_scanned(w, txid)
                txids.append(txid)
            return txids

        print()
        print('Checking z_sendmany(taddr->Sapling): change is shielded')
        # Not a fully-transparent flow, so there must be no transparent change:
        # the only transparent output would be the recipient, and the recipient
        # here is shielded, so there are no transparent outputs at all.
        txid1, _txid2 = send_twice(ua_shielded, PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
        assert_equal(transparent_output_addresses(node, txid1), [])

        print()
        print('Checking z_sendmany(taddr->taddr): change address is not reused')
        txid1, txid2 = send_twice(taddr_target, PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)

        # Each send is fully transparent, so each has exactly two outputs: the
        # recipient and the transparent change (asserted by the helper).
        change1 = transparent_change_address(node, txid1, taddr_target)
        change2 = transparent_change_address(node, txid2, taddr_target)
        print('Source address:     %s' % taddr_source)
        print('TX1 change address: %s' % change1)
        print('TX2 change address: %s' % change2)

        assert_true(
            change1 != change2,
            "the two transactions must use different change addresses")
        for change in (change1, change2):
            assert_true(
                change not in (taddr_source, taddr_target),
                "change must go to an internal address, not the source or target")

        print()
        print('All change-address tests passed!')


if __name__ == '__main__':
    WalletChangeAddressesTest().main()
