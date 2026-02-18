#!/usr/bin/env python3
# Copyright (c) 2014-2016 The Bitcoin Core developers
# Copyright (c) 2018-2023 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test RPC call failure cases. Tests should mostly correspond to code in rpc/server.cpp.
#

from test_framework.authproxy import JSONRPCException
from test_framework.test_framework import BitcoinTestFramework

class BlockchainTest(BitcoinTestFramework):
    """
    Test RPC call failure cases.
    """

    def __init__(self):
        super().__init__()
        self.num_nodes = 2
        self.num_wallets = 0

    def run_test(self):
        node = self.nodes[0]

        try:
            node.gettxoutsetinfo(1)
        except JSONRPCException as e:
            errorString = e.error['message']
        assert("Too many parameters for method `gettxoutsetinfo`. Needed exactly 0, but received 1" in errorString)

if __name__ == '__main__':
    BlockchainTest().main()
