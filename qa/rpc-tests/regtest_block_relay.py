#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .
#
# Regression test for https://github.com/ZcashFoundation/zebra/issues/10332
#
# Verifies that blocks mined on node0 propagate through a linear A-B-C topology
# to node2, which neither mines blocks nor is directly connected to node0.
# Previously, node1 would receive the block but fail to rebroadcast it to node2.

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal, connect_nodes

class RegtestBlockRelayTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.cache_behavior = 'clean'
        self.num_nodes = 3
        self.num_wallets = 0

    def setup_network(self):
        # Start nodes without connecting them; run_test manages the topology.
        self.nodes = self.setup_nodes()
        self.zainos = self.setup_indexers()
        self.wallets = self.setup_wallets()
        self.is_network_split = False

    def run_test(self):
        # Build linear topology: node0 -- node1 -- node2
        # node0 mines, node1 relays, node2 only receives.
        connect_nodes(self.nodes[0], 1)
        connect_nodes(self.nodes[1], 2)

        self.nodes[0].generate(10)
        self.sync_all(False)

        for i in range(len(self.nodes)):
            assert_equal(self.nodes[i].getbestblockheightandhash()['height'], 10)

if __name__ == '__main__':
    RegtestBlockRelayTest().main()