#!/usr/bin/env python3
# Copyright (c) 2015-2016 The Bitcoin Core developers
# Copyright (c) 2017-2022 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Exercise the node API without a wallet.
#
# On the Z3 stack the wallet (zallet) is a separate process, so a node started
# without any wallet is the default; we just run a single node and no wallets.
#

from test_framework.test_framework import BitcoinTestFramework


class DisableWalletTest (BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.cache_behavior = 'clean'
        self.num_nodes = 1
        self.num_wallets = 0

    def run_test (self):
        # Check regression: https://github.com/bitcoin/bitcoin/issues/6963#issuecomment-154548880
        x = self.nodes[0].validateaddress('t3b1jtLvxCstdo1pJs9Tjzc5dmWyvGQSZj8')
        assert(x['isvalid'] == False)
        x = self.nodes[0].validateaddress('tmGqwWtL7RsbxikDSN26gsbicxVr2xJNe86')
        assert(x['isvalid'] == True)

if __name__ == '__main__':
    DisableWalletTest ().main ()
