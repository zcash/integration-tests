#!/usr/bin/env python3
# Copyright (c) 2025 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

from decimal import Decimal

from test_framework.config import ZebraArgs

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal, start_nodes

# Check the behaviour of the value pools and funding streams at NU6.
#
# - The funding streams are updated at NU6.
# - The lockbox pool and rewards are activated at NU6.
# - The lockbox accumulates after NU6 inside the configured range.
# - The lockbox rewrards and NU6 funding streams end after the configured range.
class PoolsTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 0
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        # Add test pre and post NU6 funding streams to the node.
        args = [ZebraArgs(
            activation_heights={"NU5": 7, "NU6": 9},
            funding_streams=[pre_nu6_funding_streams(), post_nu6_funding_streams()],
        )]

        return start_nodes(self.num_nodes, self.options.tmpdir, extra_args=args)

    def run_test(self):

        def get_value_pools(value_pools):
            pools_by_id = { pool['id']: pool for pool in value_pools }
            return (pools_by_id['transparent'],
                    pools_by_id['sprout'],
                    pools_by_id['sapling'],
                    pools_by_id['orchard'],
                    pools_by_id['lockbox'])

        def get_network_upgrades(getblockchaininfo):
            upgrades_by_name = {
                upgrade['name']: {
                    k: v for k, v in upgrade.items() if k != 'name'
                }
                for upgrade in getblockchaininfo['upgrades'].values()
            }
            return (upgrades_by_name['Overwinter'],
                    upgrades_by_name['Sapling'],
                    upgrades_by_name['Blossom'],
                    upgrades_by_name['Heartwood'],
                    upgrades_by_name['Canopy'],
                    upgrades_by_name['NU5'],
                    upgrades_by_name['NU6'])

        def assert_value_pools_equals(pool1,  pool2):
            (transparent_pool1, sapling_pool1, sprout_pool1, orchard_pool1, deferred_pool1) = get_value_pools(pool1)
            (transparent_pool2, sapling_pool2, sprout_pool2, orchard_pool2, deferred_pool2) = get_value_pools(pool1)

            assert_equal(transparent_pool1['chainValue'], transparent_pool2['chainValue'])
            assert_equal(sapling_pool1['chainValue'], sapling_pool2['chainValue'])
            assert_equal(sprout_pool1['chainValue'], sprout_pool2['chainValue'])
            assert_equal(orchard_pool1['chainValue'], orchard_pool2['chainValue'])
            assert_equal(deferred_pool1['chainValue'], deferred_pool2['chainValue'])

        print("Initial Conditions at Block 0")

        # Check all value pools are empty
        value_pools_from_getblock = self.nodes[0].getblock('0')['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblock)

        assert_equal(transparent_pool['chainValue'], Decimal('0'))
        assert_equal(sprout_pool['chainValue'], Decimal('0'))
        assert_equal(sapling_pool['chainValue'], Decimal('0'))
        assert_equal(orchard_pool['chainValue'], Decimal('0'))
        assert_equal(deferred_pool['chainValue'], Decimal('0'))

        getblockchaininfo = self.nodes[0].getblockchaininfo()
        value_pools_from_getblockchaininfo = getblockchaininfo['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblockchaininfo)

        assert_value_pools_equals(value_pools_from_getblock, value_pools_from_getblockchaininfo)

        # Check the network upgrades are all pending
        (overwinter, sapling, blossom, heartwood, canopy, nu5, nu6) = get_network_upgrades(getblockchaininfo)

        assert_equal(overwinter['status'], 'pending')
        assert_equal(sapling['status'], 'pending')
        assert_equal(blossom['status'], 'pending')
        assert_equal(heartwood['status'], 'pending')
        assert_equal(canopy['status'], 'pending')
        assert_equal(nu5['status'], 'pending')
        assert_equal(nu6['status'], 'pending')

        print("Activating Overwinter, Sapling, Blossom, Heartwood and Canopy at Block 1")
        self.nodes[0].generate(1)

        # Check that the transparent pool is the only one with value
        value_pools_from_getblock = self.nodes[0].getblock('1')['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblock)

        subsidy_per_block = Decimal('6.25')
        assert_equal(transparent_pool['chainValue'], subsidy_per_block)
        assert_equal(sprout_pool['chainValue'], Decimal('0'))
        assert_equal(sapling_pool['chainValue'], Decimal('0'))
        assert_equal(orchard_pool['chainValue'], Decimal('0'))
        assert_equal(deferred_pool['chainValue'], Decimal('0'))

        getblockchaininfo = self.nodes[0].getblockchaininfo()
        value_pools_from_getblockchaininfo = getblockchaininfo['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblockchaininfo)

        assert_value_pools_equals(value_pools_from_getblock, value_pools_from_getblockchaininfo)

        # Check the network upgrades up to Canopy are active
        (overwinter, sapling, blossom, heartwood, canopy, nu5, nu6) = get_network_upgrades(getblockchaininfo)

        assert_equal(overwinter['status'], 'active')
        assert_equal(sapling['status'], 'active')
        assert_equal(blossom['status'], 'active')
        assert_equal(heartwood['status'], 'active')
        assert_equal(canopy['status'], 'active')
        assert_equal(nu5['status'], 'pending')
        assert_equal(nu6['status'], 'pending')

        print("Activating NU5 at Block 7")
        self.nodes[0].generate(6)

        # Check that the only value pool with value is still the transparent and nothing else
        value_pools_from_getblock = self.nodes[0].getblock('7')['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblock)

        assert_equal(transparent_pool['chainValue'], 7 * subsidy_per_block)
        assert_equal(sprout_pool['chainValue'], Decimal('0'))
        assert_equal(sapling_pool['chainValue'], Decimal('0'))
        assert_equal(orchard_pool['chainValue'], Decimal('0'))
        assert_equal(deferred_pool['chainValue'], Decimal('0'))

        getblockchaininfo = self.nodes[0].getblockchaininfo()
        value_pools_from_getblockchaininfo = getblockchaininfo['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblockchaininfo)

        assert_value_pools_equals(value_pools_from_getblock, value_pools_from_getblockchaininfo)

        # Check that NU5 is now active
        (overwinter, sapling, blossom, heartwood, canopy, nu5, nu6) = get_network_upgrades(getblockchaininfo)

        assert_equal(overwinter['status'], 'active')
        assert_equal(sapling['status'], 'active')
        assert_equal(blossom['status'], 'active')
        assert_equal(heartwood['status'], 'active')
        assert_equal(canopy['status'], 'active')
        assert_equal(nu5['status'], 'active')
        assert_equal(nu6['status'], 'pending')
    
        # Check we have fundingstream rewards but no lockbox rewards yet
        fs_outputs = Decimal('1.25')
        block_subsidy = self.nodes[0].getblocksubsidy()
        assert_equal(block_subsidy['miner'], subsidy_per_block - fs_outputs)
        assert_equal(block_subsidy['founders'], Decimal('0'))
        assert_equal(block_subsidy['fundingstreamstotal'], fs_outputs)
        assert_equal(block_subsidy['lockboxtotal'], Decimal('0'))
        assert_equal(block_subsidy['totalblocksubsidy'], subsidy_per_block)

        print("Activating NU6")
        self.nodes[0].generate(2)

        # Check the deferred pool has value now
        value_pools_from_getblock = self.nodes[0].getblock('9')['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblock)

        deferred_value = Decimal('0.75')
        assert_equal(transparent_pool['chainValue'], 9 * subsidy_per_block - deferred_value)
        assert_equal(sprout_pool['chainValue'], Decimal('0'))
        assert_equal(sapling_pool['chainValue'], Decimal('0'))
        assert_equal(orchard_pool['chainValue'], Decimal('0'))
        assert_equal(deferred_pool['chainValue'], deferred_value)

        getblockchaininfo = self.nodes[0].getblockchaininfo()
        value_pools_from_getblockchaininfo = getblockchaininfo['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblockchaininfo)

        assert_value_pools_equals(value_pools_from_getblock, value_pools_from_getblockchaininfo)

        # Check all upgrades up to NU6 are active
        (overwinter, sapling, blossom, heartwood, canopy, nu5, nu6) = get_network_upgrades(getblockchaininfo)

        assert_equal(overwinter['status'], 'active')
        assert_equal(sapling['status'], 'active')
        assert_equal(blossom['status'], 'active')
        assert_equal(heartwood['status'], 'active')
        assert_equal(canopy['status'], 'active')
        assert_equal(nu5['status'], 'active')
        assert_equal(nu6['status'], 'active')

        # Check that we have fundingstreams and lockbox rewards
        fs_outputs = Decimal('0.5')
        block_subsidy = self.nodes[0].getblocksubsidy()
        assert_equal(block_subsidy['miner'], subsidy_per_block - fs_outputs - deferred_value)
        assert_equal(block_subsidy['founders'], Decimal('0'))
        assert_equal(block_subsidy['fundingstreamstotal'], fs_outputs)
        assert_equal(block_subsidy['lockboxtotal'], deferred_value)
        assert_equal(block_subsidy['totalblocksubsidy'], subsidy_per_block)

        print("Pass NU6 by one block, tip now at Block 10, inside the range of the lockbox rewards")
        self.nodes[0].generate(1)

        # Check the deferred pool has more value now
        value_pools_from_getblock = self.nodes[0].getblock('10')['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblock)

        assert_equal(transparent_pool['chainValue'], 10 * subsidy_per_block - 2 * deferred_value)
        assert_equal(sprout_pool['chainValue'], Decimal('0'))
        assert_equal(sapling_pool['chainValue'], Decimal('0'))
        assert_equal(orchard_pool['chainValue'], Decimal('0'))
        assert_equal(deferred_pool['chainValue'], 2 * deferred_value)

        getblockchaininfo = self.nodes[0].getblockchaininfo()
        value_pools_from_getblockchaininfo = getblockchaininfo['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblockchaininfo)

        assert_value_pools_equals(value_pools_from_getblock, value_pools_from_getblockchaininfo)

        print("Pass the range of the lockbox, tip now at Block 12")
        self.nodes[0].generate(2)

        # Check the final deferred pool remains the same (locked until NU6.1)
        value_pools_from_getblock = self.nodes[0].getblock('12')['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblock)

        assert_equal(transparent_pool['chainValue'], 12 * subsidy_per_block - 2 * deferred_value)
        assert_equal(sprout_pool['chainValue'], Decimal('0'))
        assert_equal(sapling_pool['chainValue'], Decimal('0'))
        assert_equal(orchard_pool['chainValue'], Decimal('0'))
        assert_equal(deferred_pool['chainValue'], 2 * deferred_value)

        getblockchaininfo = self.nodes[0].getblockchaininfo()
        value_pools_from_getblockchaininfo = getblockchaininfo['valuePools']
        (transparent_pool, sapling_pool, sprout_pool, orchard_pool, deferred_pool) = get_value_pools(value_pools_from_getblockchaininfo)

        assert_value_pools_equals(value_pools_from_getblock, value_pools_from_getblockchaininfo)

        # Check there are no fundingstreams or lockbox rewards after the range
        block_subsidy = self.nodes[0].getblocksubsidy()
        assert_equal(block_subsidy['miner'], subsidy_per_block)
        assert_equal(block_subsidy['founders'], Decimal('0'))
        assert_equal(block_subsidy['fundingstreamstotal'], Decimal('0'))
        assert_equal(block_subsidy['lockboxtotal'], Decimal('0'))
        assert_equal(block_subsidy['totalblocksubsidy'], subsidy_per_block)
        
def pre_nu6_funding_streams() : return {
    'recipients': [
        {
            'receiver': 'ECC',
            'numerator': 7,
            'addresses': ['t26ovBdKAJLtrvBsE2QGF4nqBkEuptuPFZz']
        },
        {
            'receiver': 'ZcashFoundation',
            'numerator': 5,
            'addresses': ['t27eWDgjFYJGVXmzrXeVjnb5J3uXDM9xH9v']
        },
        {
            'receiver': 'MajorGrants',
            'numerator': 8,
            'addresses': ['t2Gvxv2uNM7hbbACjNox4H6DjByoKZ2Fa3P']
        },
    ],
    'height_range': {
        'start': 7,
        'end': 9
    }
}

def post_nu6_funding_streams() : return {
    'recipients': [
        {
            'receiver': 'MajorGrants',
            'numerator': 8,
            'addresses': ['t2Gvxv2uNM7hbbACjNox4H6DjByoKZ2Fa3P']
        },
        {
            'receiver': 'Deferred',
            'numerator': 12
            # No addresses field is valid for Deferred
        }
    ],
    'height_range': {
        'start': 9,
        'end': 11
    }
}

if __name__ == '__main__':
    PoolsTest().main()
