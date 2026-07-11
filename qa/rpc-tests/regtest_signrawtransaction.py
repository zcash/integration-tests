#!/usr/bin/env python3
# Copyright (c) 2018-2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Spending a transparent input signs with the correct consensus branch id.
#
# A transparent input is signed over a sighash that commits to the consensus
# branch id of the target height. If the wrong branch id is selected the
# signature does not verify and the transaction fails to build, so a successful
# transparent spend is itself the assertion.
#
# Migrated from the zcashd test to the Z3 stack (zebrad + zaino + zallet). The
# name is historical: zallet has no `signrawtransaction`, and signs transparent
# inputs internally while building the transaction. Two changes from zcashd:
#
#   * Funding the transparent address uses `z_shieldcoinbase` followed by a
#     shielded -> transparent send, because coinbase cannot be spent
#     transparently and `sendtoaddress` does not exist.
#
#   * The spend uses `AllowRevealedSenders` rather than `AllowFullyTransparent`:
#     the recipient is shielded and zallet shields the change of a non-fully-
#     transparent flow, so nothing transparent is revealed except the sender.
#

from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    Pool,
    PrivacyPolicy,
    assert_equal,
    first_transparent_receiver,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_stable_transparent,
    wait_for_tx_scanned,
)

FUND_AMOUNT = Decimal('2')
SEND_AMOUNT = Decimal('1')

RECEIVERS = ['orchard', 'p2pkh']
DIVERSIFIER_SHIELDED = 10
DIVERSIFIER_TRANSPARENT = 11


def _ua_for(wallet, acct, diversifier_index):
    return wallet.z_getaddressforaccount(
        acct, RECEIVERS, diversifier_index)['address']


class RegtestSignrawtransactionTest(BitcoinTestFramework):

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

        ua_shielded = _ua_for(w, acct, DIVERSIFIER_SHIELDED)
        taddr = first_transparent_receiver(
            w, _ua_for(w, acct, DIVERSIFIER_TRANSPARENT))

        # Shield coinbase, so there are shielded funds to unshield.
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

        # Fund the transparent address (its UTXO is non-coinbase, so spendable).
        opid = w.z_sendmany(
            ua_shielded,
            [{'address': taddr, 'amount': FUND_AMOUNT}],
            1,
            None,
            PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        fund_txid = wait_and_assert_operationid_status(w, opid)
        node.generate(1)
        wait_for_tx_scanned(w, fund_txid)
        wait_for_stable_transparent(w, min_count=1)
        assert_equal(node.getaddressbalance(taddr)['balance'],
                     int(FUND_AMOUNT * COIN))

        # Spend the transparent input. If the wrong consensus branch id were used
        # to sign it, this would fail to build.
        opid = w.z_sendmany(
            taddr,
            [{'address': ua_shielded, 'amount': SEND_AMOUNT}],
            1,
            None,
            PrivacyPolicy.ALLOW_REVEALED_SENDERS)
        spend_txid = wait_and_assert_operationid_status(w, opid)
        node.generate(1)
        wait_for_tx_scanned(w, spend_txid)

        # The transparent input was consumed, so it was signed and accepted.
        assert_equal(node.getaddressbalance(taddr)['balance'], 0)
        print("Spent a transparent input in %s; signature verified under the "
              "target height's consensus branch id." % spend_txid)


if __name__ == '__main__':
    RegtestSignrawtransactionTest().main()
