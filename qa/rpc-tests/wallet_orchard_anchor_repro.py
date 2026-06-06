#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""
Reproduce the one-wallet Orchard anchor handling issue encountered while
bringing up the gRPC comparison fixture.

The gRPC fixture avoids this shape by splitting Sapling and Orchard transaction
authoring across separate standalone zcashd wallets. This test intentionally
keeps the sequence inside one zcashd wallet:

1. Mine transparent coinbase funds.
2. Create a Sapling note.
3. Spend Sapling -> Orchard.
4. Immediately spend that Orchard note.

On affected zcashd versions, the final Orchard spend fails because the wallet
does not make the just-created Orchard note available as spendable to the same
wallet process. During the gRPC fixture work this surfaced in the wallet
anchor-handling path; in this focused repro the user-visible RPC error is an
insufficient-funds failure at the follow-on Orchard spend.
"""

from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    start_zcashd_node,
    stop_zcashd_node,
    wait_and_assert_operationid_status,
)
from test_framework.zip317 import conventional_fee


ZCASHD_NUPARAMS = {
    '5ba81b19': 1,  # Overwinter
    '76b809bb': 1,  # Sapling
    '2bb40e60': 1,  # Blossom
    'f5b9230b': 1,  # Heartwood
    'e9ff75a6': 1,  # Canopy
    'c2d6d0b4': 2,  # NU5
    'c8e71055': 2,  # NU6
}


class WalletOrchardAnchorReproTest(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.num_nodes = 0
        self.num_indexers = 0
        self.num_lightwalletds = 0
        self.num_wallets = 0
        self.num_zcashd_nodes = 1
        self.cache_behavior = 'clean'

    def setup_chain(self):
        # This repro uses a standalone zcashd datadir built from scratch.
        pass

    def setup_network(self, split=False):
        self.nodes = []
        self.zainos = []
        self.lwds = []
        self.wallets = []
        self.zcashd_nodes = [
            start_zcashd_node(
                0,
                self.options.tmpdir,
                activation_heights=ZCASHD_NUPARAMS,
            )
        ]

    def run_test(self):
        node = self.zcashd_nodes[0]

        # Height 200 gives us mature transparent coinbase UTXOs. NU5/NU6 are
        # already active from height 2, so the shielded sequence below can use
        # Orchard immediately.
        node.generate(200)
        assert_equal(node.getblockcount(), 200)

        sapling_account = node.z_getnewaccount()['account']
        sapling_ua = node.z_getaddressforaccount(sapling_account, ['sapling'])['address']

        orchard_account = node.z_getnewaccount()['account']
        orchard_ua = node.z_getaddressforaccount(orchard_account, ['orchard'])['address']
        orchard_addr = node.z_listunifiedreceivers(orchard_ua)['orchard']

        recipient_account = node.z_getnewaccount()['account']
        recipient_ua = node.z_getaddressforaccount(recipient_account, ['orchard'])['address']
        recipient_addr = node.z_listunifiedreceivers(recipient_ua)['orchard']

        # Fund the account from transparent coinbase. Coinbase UTXOs are
        # shielded via z_shieldcoinbase so this repro does not depend on
        # transparent-change policy.
        sapling_fee = conventional_fee(13)
        result = node.z_shieldcoinbase("*", sapling_ua, sapling_fee, 10)
        wait_and_assert_operationid_status(node, result['opid'])
        node.generate(2)
        stop_zcashd_node(0, node)
        node = start_zcashd_node(
            0,
            self.options.tmpdir,
            activation_heights=ZCASHD_NUPARAMS,
        )
        self.zcashd_nodes[0] = node
        balance = node.z_getbalanceforaccount(sapling_account)
        assert_equal(balance['pools']['sapling']['valueZat'] > 0, True)

        # Create the Orchard note via a cross-pool Sapling -> Orchard spend.
        orchard_amount = Decimal('0.5')
        cross_pool_fee = conventional_fee(4)
        opid = node.z_sendmany(
            sapling_ua,
            [{"address": orchard_addr, "amount": orchard_amount}],
            1,
            cross_pool_fee,
            'AllowRevealedAmounts',
        )
        wait_and_assert_operationid_status(node, opid)
        node.generate(1)

        # Follow-on Orchard spend. The expected wallet behavior is that the
        # Orchard note created by the previous transaction is immediately
        # spendable by this same wallet. Affected zcashd versions fail here
        # with an insufficient-funds async operation error.
        opid = node.z_sendmany(
            orchard_ua,
            [{"address": recipient_addr, "amount": Decimal('0.1')}],
            1,
            conventional_fee(2),
            'AllowRevealedAmounts',
        )
        wait_and_assert_operationid_status(node, opid)


if __name__ == '__main__':
    WalletOrchardAnchorReproTest().main()
