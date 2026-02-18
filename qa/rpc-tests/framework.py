#!/usr/bin/env python3
# Copyright (c) 2020 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_raises,
    connect_nodes,
    start_node,
    check_node_log,
)

class FrameworkTest (BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 2
        self.num_wallets = 0
        self.cache_behavior = 'clean'

    def start_node_with(self, index, extra_args=[]):
        args = []
        return start_node(index, self.options.tmpdir, args + extra_args)

    def run_test (self):

        # Test the check_node_log utility function
        string_to_find = "Zcash version"
        check_node_log(self, 1, string_to_find)

        # Node 1 was stopped to check the logs, need to be restarted
        self.nodes[1] = self.start_node_with(1, [])
        connect_nodes(self.nodes[1], 0)

        assert_raises(AssertionError, check_node_log, self, 1, "Will not be found")

        # Need to start node 1 before leaving the test
        self.nodes[1] = self.start_node_with(1, [])
        connect_nodes(self.nodes[1], 0)


if __name__ == '__main__':
    FrameworkTest().main()
