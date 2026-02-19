#!/usr/bin/env python3
# Copyright (c) 2025 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal

# Test that we can call the indexer RPCs.
class IndexerTest (BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.cache_behavior = 'clean'
        self.num_nodes = 1
        self.num_indexers = 1
        self.num_wallets = 0

    def run_test(self):
        assert_equal(self.zainos[0].getblockcount(), 100)
        assert_equal(self.nodes[0].getblockcount(), 100)

if __name__ == '__main__':
    IndexerTest ().main ()
