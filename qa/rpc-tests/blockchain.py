#!/usr/bin/env python3
# Copyright (c) 2014-2016 The Bitcoin Core developers
# Copyright (c) 2018-2022 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test RPC calls related to blockchain state. Tests correspond to code in
# rpc/blockchain.cpp.
#

import decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
)

class BlockchainTest(BitcoinTestFramework):
    """
    Test blockchain-related RPC calls:

        - gettxoutsetinfo

    """

    def __init__(self):
        super().__init__()
        self.num_nodes = 2
        self.num_wallets = 0

    def run_test(self):
        node = self.nodes[0]
        res = node.gettxoutsetinfo()

        assert_equal(res['total_amount'], decimal.Decimal('2143.75000000')) # 144*12.5 + 55*6.25
        assert_equal(res['transactions'], 200)
        assert_equal(res['height'], 200)
        assert_equal(res['txouts'], 343) # 144*2 + 55
        assert_equal(res['bytes_serialized'], 14819), # 32*199 + 48*90 + 49*54 + 27*55
        assert_equal(len(res['bestblock']), 64)
        assert_equal(len(res['hash_serialized']), 64)


if __name__ == '__main__':
    BlockchainTest().main()
