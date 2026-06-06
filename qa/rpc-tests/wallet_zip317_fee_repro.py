#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    get_coinbase_address,
    start_zcashd_node,
    stop_zcashd_node,
    wait_and_assert_operationid_status,
)
from test_framework.zip317 import ZIP_317_FEE, conventional_fee


_REPRO_ACTIVATION_HEIGHTS = {
    '5ba81b19': 1,  # Overwinter
    '76b809bb': 1,  # Sapling
    '2bb40e60': 1,  # Blossom
    'f5b9230b': 1,  # Heartwood
    'e9ff75a6': 1,  # Canopy
    'c2d6d0b4': 2,  # NU5
    'c8e71055': 2,  # NU6
}


def _submit_missing_blocks(src_node, dst_node):
    """Submit any blocks missing from dst_node using raw blocks from src_node."""
    dst_height = dst_node.getblockcount()
    src_height = src_node.getblockcount()
    for height in range(dst_height + 1, src_height + 1):
        raw_hex = src_node.getblock(str(height), 0)
        result = dst_node.submitblock(raw_hex)
        if result is not None:
            raise AssertionError("submitblock to repro node failed at height %d: %s" % (height, result))


class WalletZip317FeeReproTest(BitcoinTestFramework):
    """
    Focused repro for the zcashd ZIP 317 fee-estimation bug surfaced by the
    grpc_comparison fixture.

    This mirrors the specific standalone-wallet path that reached the bug in
    grpc_comparison.py:
    - node 0 mines transparent coinbase funds,
    - node 0 creates a Sapling-only source UA,
    - node 1 owns an Orchard-only recipient UA,
    - node 0 sends Sapling -> Orchard with AllowRevealedAmounts.

    With ZIP_317_FEE, zcashd returns an opid but later fails the async
    operation with "tx unpaid action limit exceeded". With conventional_fee(4),
    the same transaction shape succeeds.
    """

    def __init__(self):
        super().__init__()
        self.num_nodes = 0
        self.num_wallets = 0

    def setup_chain(self):
        pass

    def setup_network(self, split=False):
        self.nodes = []
        self.wallets = []
        self.zainos = []
        self.lwds = []
        self.zcashd_nodes = [
            start_zcashd_node(0, self.options.tmpdir, activation_heights=_REPRO_ACTIVATION_HEIGHTS),
            start_zcashd_node(1, self.options.tmpdir, activation_heights=_REPRO_ACTIVATION_HEIGHTS),
        ]
        self.is_network_split = False

    def _mine_and_sync(self, node0, node1):
        node0.generate(1)
        _submit_missing_blocks(node0, node1)
        return node0.getblockcount()

    def _restart_node0(self):
        stop_zcashd_node(0, self.zcashd_nodes[0])
        self.zcashd_nodes[0] = start_zcashd_node(0, self.options.tmpdir, activation_heights=_REPRO_ACTIVATION_HEIGHTS)
        return self.zcashd_nodes[0]

    def run_test(self):
        node0, node1 = self.zcashd_nodes

        node0.generate(200)
        _submit_missing_blocks(node0, node1)
        assert_equal(node0.getblockcount(), 200)
        assert_equal(node1.getblockcount(), 200)

        taddr = get_coinbase_address(node0)
        source_account = node0.z_getnewaccount()['account']
        source_ua = node0.z_getaddressforaccount(source_account, ['sapling', 'orchard'])['address']
        source_sapling = node0.z_listunifiedreceivers(source_ua)['sapling']

        orchard_account = node1.z_getnewaccount()['account']
        orchard_ua = node1.z_getaddressforaccount(orchard_account, ['orchard'])['address']
        orchard_addr = node1.z_listunifiedreceivers(orchard_ua)['orchard']

        funding_fee = conventional_fee(4)
        funding_amount = Decimal('12.5') - funding_fee
        funding_txid = wait_and_assert_operationid_status(
            node0,
            node0.z_sendmany(
                taddr,
                [{"address": source_sapling, "amount": funding_amount}],
                10,
                funding_fee,
                'AllowRevealedSenders',
            ),
        )
        assert_equal(len(funding_txid), 64)
        assert_equal(self._mine_and_sync(node0, node1), 201)
        assert_equal(self._mine_and_sync(node0, node1), 202)
        node0 = self._restart_node0()
        assert_equal(node0.getblockcount(), 202)

        recipients = [{"address": orchard_addr, "amount": Decimal('0.1')}]
        buggy_opid = node0.z_sendmany(
            source_ua,
            recipients,
            1,
            ZIP_317_FEE,
            'AllowRevealedAmounts',
        )
        try:
            wait_and_assert_operationid_status(node0, buggy_opid)
        except AssertionError as e:
            message = str(e)
            assert "tx unpaid action limit exceeded: 1 action(s) exceeds limit of 0" in message, message
        else:
            raise AssertionError(
                "Expected ZIP_317_FEE Sapling->Orchard send to fail with an unpaid action error"
            )

        workaround_fee = conventional_fee(4)
        workaround_txid = wait_and_assert_operationid_status(
            node0,
            node0.z_sendmany(
                source_ua,
                recipients,
                1,
                workaround_fee,
                'AllowRevealedAmounts',
            ),
        )
        assert_equal(len(workaround_txid), 64)
        assert_equal(self._mine_and_sync(node0, node1), 203)


if __name__ == '__main__':
    WalletZip317FeeReproTest().main()
