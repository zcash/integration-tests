#!/usr/bin/env python3
# Copyright (c) 2021 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from test_framework.test_framework import BitcoinTestFramework


class GetMiningInfoTest(BitcoinTestFramework):
    '''
    Test getmininginfo.
    '''

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 0
        self.cache_behavior = 'clean'

    def run_test(self):
        node = self.nodes[0]

        info = node.getmininginfo()
        assert(info['blocks'] == 0)
        # No blocks have been mined yet, so these fields should not be present.
        assert('currentblocksize' not in info)
        assert('currentblocktx' not in info)

        node.generate(1)

        info = node.getmininginfo()
        assert(info['blocks'] == 1)
        # One block has been mined, so these fields should now be present.
        assert('currentblocksize' in info)
        assert('currentblocktx' in info)
        assert(info['currentblocksize'] > 0)
        # The transaction count doesn't include the coinbase
        assert(info['currentblocktx'] == 0)


if __name__ == '__main__':
    GetMiningInfoTest().main()
