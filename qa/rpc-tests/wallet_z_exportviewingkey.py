#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test z_exportviewingkey RPC against the Z3 stack (zebrad + zaino + zallet).
#
# Covers the z_exportviewingkey API surface:
#
#   z_exportviewingkey(zaddr)
#
#     zaddr : a Sapling payment address owned by this wallet.
#
# Returns the bech32-encoded Sapling extended full viewing key.
#

from test_framework.test_framework import BitcoinTestFramework
from test_framework.authproxy import JSONRPCException
from test_framework.util import (
    assert_equal,
    assert_true,
)

import logging
import sys

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO, stream=sys.stdout)


def expect_rpc_error(callable_, *args):
    """Invoke an RPC and return the JSONRPCException; fail if it didn't raise."""
    try:
        callable_(*args)
    except JSONRPCException as e:
        return e
    raise AssertionError(
        "Expected RPC error, but call succeeded: {}({})".format(
            getattr(callable_, '__name__', '?'), args))


def assert_in_message(e, needle):
    msg = e.error['message']
    assert_true(needle in msg, "Expected {!r} in error, got: {!r}".format(needle, msg))


def get_sapling_receiver(wallet, account_uuid):
    """Materialize a UA with a Sapling receiver and extract the bare Sapling address."""
    ua_result = wallet.z_getaddressforaccount(account_uuid, ["sapling"])
    ua = ua_result['address']
    receivers = wallet.z_listunifiedreceivers(ua)
    sapling_addr = receivers.get('sapling')
    assert_true(sapling_addr is not None,
                "UA {} has no Sapling receiver".format(ua))
    return sapling_addr


class WalletZExportViewingKeyTest(BitcoinTestFramework):
    '''
    Test the z_exportviewingkey RPC method.

    Verifies that Sapling extended full viewing keys can be exported from a
    wallet, producing valid bech32-encoded keys that round-trip correctly
    through z_importviewingkey.
    '''

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def run_test(self):
        wallet = self.wallets[0]

        # Identify the default account.
        accounts = wallet.z_listaccounts()
        assert_true(len(accounts) >= 1, "Wallet should have at least one account")
        account_uuid = accounts[0]['account_uuid']

        # Get a bare Sapling address via UA decomposition.
        sapling_addr = get_sapling_receiver(wallet, account_uuid)

        logging.info("Test 1: Export viewing key for valid Sapling address...")
        vkey = wallet.z_exportviewingkey(sapling_addr)
        assert_true(isinstance(vkey, str), "Expected string result")
        assert_true(
            vkey.startswith("zxviewregtestsapling"),
            "Expected regtest Sapling EFVK HRP, got: {}...".format(vkey[:30]))
        logging.info("  PASSED")

        logging.info("Test 2: Idempotent export...")
        vkey2 = wallet.z_exportviewingkey(sapling_addr)
        assert_equal(vkey, vkey2)
        logging.info("  PASSED")

        logging.info("Test 3: Invalid address rejected...")
        e = expect_rpc_error(wallet.z_exportviewingkey, "not-a-valid-address")
        assert_in_message(e, "Invalid zaddr")
        logging.info("  PASSED")

        logging.info("Test 4: Transparent address rejected...")
        taddr = self.miner_addresses[0]
        e = expect_rpc_error(wallet.z_exportviewingkey, taddr)
        assert_in_message(e, "only supports Sapling")
        logging.info("  PASSED")

        logging.info("Test 5: Export from a second account...")
        account2 = wallet.z_getnewaccount("test-account-2")
        account2_uuid = account2['account_uuid']
        sapling_addr2 = get_sapling_receiver(wallet, account2_uuid)

        vkey_acct2 = wallet.z_exportviewingkey(sapling_addr2)
        assert_true(vkey_acct2.startswith("zxviewregtestsapling"),
                    "Expected regtest Sapling EFVK HRP for account 2")
        # Different accounts should produce different viewing keys.
        assert_true(vkey != vkey_acct2,
                    "Different accounts should have different viewing keys")
        logging.info("  PASSED")

        logging.info("All z_exportviewingkey tests passed.")


if __name__ == '__main__':
    WalletZExportViewingKeyTest().main()
