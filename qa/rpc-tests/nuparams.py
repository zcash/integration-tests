#!/usr/bin/env python3
# Copyright (c) 2021 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from test_framework.config import ZebraArgs
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    start_nodes,
    nustr,
    OVERWINTER_BRANCH_ID,
    SAPLING_BRANCH_ID,
    BLOSSOM_BRANCH_ID,
    HEARTWOOD_BRANCH_ID,
    CANOPY_BRANCH_ID,
    NU5_BRANCH_ID,
    NU6_BRANCH_ID,
)
from decimal import Decimal


class NuparamsTest(BitcoinTestFramework):
    '''
    Test that unspecified network upgrades are activated automatically;
    this is really more of a test of the test framework.
    '''

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 0
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        args = [ZebraArgs(
            activation_heights={"NU5": 7, "NU6": 9},
        )]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def run_test(self):
        node = self.nodes[0]
        # No blocks have been created, only the genesis block exists (height 0)
        bci = node.getblockchaininfo()
        assert_equal(bci['blocks'], 0)
        upgrades = bci['upgrades']

        overwinter = upgrades[nustr(OVERWINTER_BRANCH_ID)]
        assert_equal(overwinter['name'], 'Overwinter')
        assert_equal(overwinter['activationheight'], 1)
        assert_equal(overwinter['status'], 'pending')

        sapling = upgrades[nustr(SAPLING_BRANCH_ID)]
        assert_equal(sapling['name'], 'Sapling')
        assert_equal(sapling['activationheight'], 1)
        assert_equal(sapling['status'], 'pending')

        blossom = upgrades[nustr(BLOSSOM_BRANCH_ID)]
        assert_equal(blossom['name'], 'Blossom')
        assert_equal(blossom['activationheight'], 1)
        assert_equal(blossom['status'], 'pending')

        heartwood = upgrades[nustr(HEARTWOOD_BRANCH_ID)]
        assert_equal(heartwood['name'], 'Heartwood')
        assert_equal(heartwood['activationheight'], 1)
        assert_equal(heartwood['status'], 'pending')

        canopy = upgrades[nustr(CANOPY_BRANCH_ID)]
        assert_equal(canopy['name'], 'Canopy')
        assert_equal(canopy['activationheight'], 1)
        assert_equal(canopy['status'], 'pending')

        nu5 = upgrades[nustr(NU5_BRANCH_ID)]
        assert_equal(nu5['name'], 'NU5')
        assert_equal(nu5['activationheight'], 7)
        assert_equal(nu5['status'], 'pending')

        nu6 = upgrades[nustr(NU6_BRANCH_ID)]
        assert_equal(nu6['name'], 'NU6')
        assert_equal(nu6['activationheight'], 9)
        assert_equal(nu6['status'], 'pending')

        # Initial subsidy at the genesis block is 12.5 ZEC
        assert_equal(node.getblocksubsidy()["miner"], Decimal("12.5"))

        # Zebra regtest mode hardcodes Canopy, Heartwood, Blossom, Sapling and Overwinter
        # to activate at height 1.
        node.generate(1)

        bci = node.getblockchaininfo()
        assert_equal(bci['blocks'], 1)
        upgrades = bci['upgrades']

        overwinter = upgrades[nustr(OVERWINTER_BRANCH_ID)]
        assert_equal(overwinter['name'], 'Overwinter')
        assert_equal(overwinter['activationheight'], 1)
        assert_equal(overwinter['status'], 'active')

        sapling = upgrades[nustr(SAPLING_BRANCH_ID)]
        assert_equal(sapling['name'], 'Sapling')
        assert_equal(sapling['activationheight'], 1)
        assert_equal(sapling['status'], 'active')

        blossom = upgrades[nustr(BLOSSOM_BRANCH_ID)]
        assert_equal(blossom['name'], 'Blossom')
        assert_equal(blossom['activationheight'], 1)
        assert_equal(blossom['status'], 'active')

        heartwood = upgrades[nustr(HEARTWOOD_BRANCH_ID)]
        assert_equal(heartwood['name'], 'Heartwood')
        assert_equal(heartwood['activationheight'], 1)
        assert_equal(heartwood['status'], 'active')

        canopy = upgrades[nustr(CANOPY_BRANCH_ID)]
        assert_equal(canopy['name'], 'Canopy')
        assert_equal(canopy['activationheight'], 1)
        assert_equal(canopy['status'], 'active')

        nu5 = upgrades[nustr(NU5_BRANCH_ID)]
        assert_equal(nu5['name'], 'NU5')
        assert_equal(nu5['activationheight'], 7)
        assert_equal(nu5['status'], 'pending')

        nu6 = upgrades[nustr(NU6_BRANCH_ID)]
        assert_equal(nu6['name'], 'NU6')
        assert_equal(nu6['activationheight'], 9)
        assert_equal(nu6['status'], 'pending')

        # Block subsidy halves at Blossom due to block time halving
        # The founders' reward ends at Canopy and there are no funding streams
        # configured by default for regtest. On mainnet, the halving activated
        # coincident with Canopy, but on regtest the two are independent.
        assert_equal(node.getblocksubsidy()["miner"], Decimal("6.25"))

        # Activate NU5
        node.generate(6)
        bci = node.getblockchaininfo()
        assert_equal(bci['blocks'], 7)
        upgrades = bci['upgrades']

        overwinter = upgrades[nustr(OVERWINTER_BRANCH_ID)]
        assert_equal(overwinter['name'], 'Overwinter')
        assert_equal(overwinter['activationheight'], 1)
        assert_equal(overwinter['status'], 'active')

        sapling = upgrades[nustr(SAPLING_BRANCH_ID)]
        assert_equal(sapling['name'], 'Sapling')
        assert_equal(sapling['activationheight'], 1)
        assert_equal(sapling['status'], 'active')

        blossom = upgrades[nustr(BLOSSOM_BRANCH_ID)]
        assert_equal(blossom['name'], 'Blossom')
        assert_equal(blossom['activationheight'], 1)
        assert_equal(blossom['status'], 'active')

        heartwood = upgrades[nustr(HEARTWOOD_BRANCH_ID)]
        assert_equal(heartwood['name'], 'Heartwood')
        assert_equal(heartwood['activationheight'], 1)
        assert_equal(heartwood['status'], 'active')

        canopy = upgrades[nustr(CANOPY_BRANCH_ID)]
        assert_equal(canopy['name'], 'Canopy')
        assert_equal(canopy['activationheight'], 1)
        assert_equal(canopy['status'], 'active')

        nu5 = upgrades[nustr(NU5_BRANCH_ID)]
        assert_equal(nu5['name'], 'NU5')
        assert_equal(nu5['activationheight'], 7)
        assert_equal(nu5['status'], 'active')

        nu6 = upgrades[nustr(NU6_BRANCH_ID)]
        assert_equal(nu6['name'], 'NU6')
        assert_equal(nu6['activationheight'], 9)
        assert_equal(nu6['status'], 'pending')

        # Block subsidy remains the same after NU5
        assert_equal(node.getblocksubsidy()["miner"], Decimal("6.25"))

        # Activate NU6
        node.generate(2)
        bci = node.getblockchaininfo()
        assert_equal(bci['blocks'], 9)
        upgrades = bci['upgrades']

        overwinter = upgrades[nustr(OVERWINTER_BRANCH_ID)]
        assert_equal(overwinter['name'], 'Overwinter')
        assert_equal(overwinter['activationheight'], 1)
        assert_equal(overwinter['status'], 'active')

        sapling = upgrades[nustr(SAPLING_BRANCH_ID)]
        assert_equal(sapling['name'], 'Sapling')
        assert_equal(sapling['activationheight'], 1)
        assert_equal(sapling['status'], 'active')

        blossom = upgrades[nustr(BLOSSOM_BRANCH_ID)]
        assert_equal(blossom['name'], 'Blossom')
        assert_equal(blossom['activationheight'], 1)
        assert_equal(blossom['status'], 'active')

        heartwood = upgrades[nustr(HEARTWOOD_BRANCH_ID)]
        assert_equal(heartwood['name'], 'Heartwood')
        assert_equal(heartwood['activationheight'], 1)
        assert_equal(heartwood['status'], 'active')

        canopy = upgrades[nustr(CANOPY_BRANCH_ID)]
        assert_equal(canopy['name'], 'Canopy')
        assert_equal(canopy['activationheight'], 1)
        assert_equal(canopy['status'], 'active')

        nu5 = upgrades[nustr(NU5_BRANCH_ID)]
        assert_equal(nu5['name'], 'NU5')
        assert_equal(nu5['activationheight'], 7)
        assert_equal(nu5['status'], 'active')

        nu6 = upgrades[nustr(NU6_BRANCH_ID)]
        assert_equal(nu6['name'], 'NU6')
        assert_equal(nu6['activationheight'], 9)
        assert_equal(nu6['status'], 'active')

        # Block subsidy remains the same after NU6
        assert_equal(node.getblocksubsidy()["miner"], Decimal("6.25"))

if __name__ == '__main__':
    NuparamsTest().main()
