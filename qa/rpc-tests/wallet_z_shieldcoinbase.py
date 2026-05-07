#!/usr/bin/env python3
# Copyright (c) 2025-2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test z_shieldcoinbase RPC against the Z3 stack (zebrad + zaino + zallet).
#
# This is a NEW test that exercises Zallet's z_shieldcoinbase implementation
# directly, rather than attempting to migrate the legacy zcashd tests.
# It uses 1 node + 1 wallet to avoid the Zaino race condition with
# concurrent wallets.
#

import time

from decimal import Decimal
from test_framework.test_framework import BitcoinTestFramework
from test_framework.authproxy import JSONRPCException
from test_framework.config import ZebraArgs
from test_framework.util import (
    assert_equal,
    assert_true,
    start_nodes,
    wait_and_assert_operationid_status,
    wait_and_assert_operationid_status_result,
)

# Coinbase outputs require 100 confirmations before they can be spent.
COINBASE_MATURITY = 100


def wait_for_mature_coinbase(wallet, min_mature_utxos=1, timeout=120):
    """
    Wait until the wallet has indexed at least `min_mature_utxos` mature
    coinbase UTXOs spendable via `z_shieldcoinbase`.

    Zallet's `z_gettotalbalance` and `z_listunspent` reflect only what the
    wallet has scanned and committed to its local SQLite database. After
    `node.generate(N)`, there is a non-trivial delay (block fetch + scan +
    commit) before those views update. The framework's `sync_all` only
    synchronizes nodes, not wallets, and no `getwalletstatus` RPC is
    available yet (https://github.com/zcash/wallet/issues/316).

    Polls `z_listunspent(minconf=COINBASE_MATURITY + 1)` once per second.
    Considered ready when at least `min_mature_utxos` transparent outputs
    are visible at that confirmation depth.
    """
    deadline = time.time() + timeout
    last_count = 0
    while time.time() < deadline:
        try:
            utxos = wallet.z_listunspent(COINBASE_MATURITY + 1)
            transparent = [u for u in utxos if u.get('pool') == 'transparent']
            last_count = len(transparent)
            if last_count >= min_mature_utxos:
                return
        except Exception:
            pass
        time.sleep(1)

    raise AssertionError(
        "wait_for_mature_coinbase: timeout after {}s; only saw {} mature "
        "transparent UTXOs (wanted {})".format(
            timeout, last_count, min_mature_utxos
        )
    )


class WalletZShieldCoinbaseTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        # NU5 must be explicitly activated for both zebrad and zallet.
        # Zallet's z_shieldcoinbase hardcodes Orchard change strategy which
        # requires NU5. The default zallet.toml includes NU5 at height 1,
        # and we must match that on the zebrad side.
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights={"NU5": 1},
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def run_test(self):
        node = self.nodes[0]
        wallet = self.wallets[0]

        # The miner address for account 0 (set up by prepare_wallets_for_mining).
        miner_taddr = self.miner_addresses[0]

        print("Mining initial blocks to mature coinbase...")
        # Mine enough blocks so that the earliest coinbase UTXOs have
        # more than 100 confirmations.
        node.generate(COINBASE_MATURITY + 20)
        wait_for_mature_coinbase(wallet, min_mature_utxos=10)

        # Verify the wallet sees the transparent balance.
        wallet_balance = wallet.z_gettotalbalance(1, True)
        assert_true(
            Decimal(wallet_balance['transparent']) > Decimal('0'),
            "Wallet should see transparent balance"
        )
        print("  Balance verified: {} ZEC transparent".format(wallet_balance['transparent']))

        # Get a shielded address from account 0 (with Orchard receiver).
        addr_result = wallet.z_getaddressforaccount(0, ["orchard"])
        zaddr = addr_result['address']
        print("  Shielded address (Orchard UA): {}...".format(zaddr[:20]))

        # ================================================================
        # Error handling tests (no coinbase maturity needed)
        # ================================================================

        print("Test 1: Fee must be null...")
        try:
            wallet.z_shieldcoinbase(miner_taddr, zaddr, Decimal('0.001'))
            raise AssertionError("Should have thrown for non-null fee")
        except JSONRPCException as e:
            assert_true(
                "fee field must be null" in e.error['message'],
                "Expected fee-must-be-null error, got: " + e.error['message']
            )
        print("  PASSED")

        print("Test 2: Invalid toaddress (transparent)...")
        try:
            wallet.z_shieldcoinbase(miner_taddr, miner_taddr, None)
            raise AssertionError("Should have thrown for transparent toaddress")
        except JSONRPCException as e:
            assert_true(
                "toaddress must be a shielded address" in e.error['message'],
                "Expected shielded-address error, got: " + e.error['message']
            )
        print("  PASSED")

        print("Test 3: Invalid fromaddress...")
        try:
            wallet.z_shieldcoinbase("not_a_real_address", zaddr, None)
            raise AssertionError("Should have thrown for invalid fromaddress")
        except JSONRPCException as e:
            assert_true(
                "Invalid from address" in e.error['message'] or
                "unknown address format" in e.error['message'],
                "Expected invalid-address error, got: " + e.error['message']
            )
        print("  PASSED")

        print("Test 4: Shielded fromaddress rejected...")
        try:
            wallet.z_shieldcoinbase(zaddr, zaddr, None)
            raise AssertionError("Should have thrown for shielded fromaddress")
        except JSONRPCException as e:
            assert_true(
                "only supports transparent source" in e.error['message'] or
                "should be a taddr" in e.error['message'],
                "Expected transparent-only error, got: " + e.error['message']
            )
        print("  PASSED")

        print("Test 5: LegacyCompat privacy policy rejected...")
        try:
            wallet.z_shieldcoinbase(miner_taddr, zaddr, None, None, None, "LegacyCompat")
            raise AssertionError("Should have thrown for LegacyCompat")
        except JSONRPCException as e:
            assert_true(
                "LegacyCompat" in e.error['message'],
                "Expected LegacyCompat rejection, got: " + e.error['message']
            )
        print("  PASSED")

        print("Test 6: Same-account enforcement...")
        account2 = wallet.z_getnewaccount("test-account-2")
        account2_uuid = account2['account_uuid']
        addr2_result = wallet.z_getaddressforaccount(account2_uuid, ["orchard"])
        zaddr2 = addr2_result['address']
        try:
            wallet.z_shieldcoinbase(miner_taddr, zaddr2, None)
            raise AssertionError("Should have thrown for cross-account shielding")
        except JSONRPCException as e:
            assert_true(
                "same account" in e.error['message'].lower() or
                "fromaddress and toaddress must belong to the same account" in e.error['message'],
                "Expected same-account error, got: " + e.error['message']
            )
        print("  PASSED")

        # ================================================================
        # Shielding transaction tests (require mature coinbase)
        # ================================================================

        print("Test 7: Basic shielding (explicit taddr -> Orchard UA)...")
        result = wallet.z_shieldcoinbase(miner_taddr, zaddr, None)
        # The pre-flight return shape matches zcashd:
        # { remainingUTXOs, remainingValue, shieldingUTXOs, shieldingValue, opid }
        assert_true(isinstance(result, dict),
                    "Expected response object, got: {}".format(type(result)))
        for key in ('remainingUTXOs', 'remainingValue', 'shieldingUTXOs',
                    'shieldingValue', 'opid'):
            assert_true(key in result,
                        "Missing field '{}' in response: {}".format(key, result))
        assert_true(result['shieldingUTXOs'] > 0,
                    "Expected at least one UTXO being shielded, got: {}".format(
                        result['shieldingUTXOs']))
        assert_true(Decimal(result['shieldingValue']) > Decimal('0'),
                    "Expected positive shielding value, got: {}".format(
                        result['shieldingValue']))
        # `limit` is currently ignored by Zallet, so remainingUTXOs is
        # always 0 on success. This invariant will change when limit is
        # honored (https://github.com/zcash/wallet/issues/NNN).
        assert_equal(result['remainingUTXOs'], 0)
        opid = result['opid']
        assert_true(isinstance(opid, str),
                    "Expected opid string, got: {}".format(type(opid)))
        assert_true(opid.startswith("opid-"),
                    "Expected opid format, got: {}".format(opid))

        txid = wait_and_assert_operationid_status(wallet, opid)
        assert_true(txid is not None, "Shielding transaction should have succeeded")
        print("  Shielding tx: {}".format(txid))

        # Mine a block to confirm the shielding transaction, then wait for
        # the wallet to scan it.
        node.generate(1)
        wait_for_mature_coinbase(wallet)

        # Verify the wallet sees the shielded balance.
        wallet_balance = wallet.z_gettotalbalance(1, True)
        shielded = Decimal(wallet_balance['private'])
        print("  Shielded balance: {} ZEC".format(shielded))
        assert_true(shielded > Decimal('0'), "Shielded balance should be positive after shielding")
        print("  PASSED")

        print("Test 8: Wildcard shielding (* -> Orchard UA)...")
        # Mine blocks to create AND mature new coinbase UTXOs.
        node.generate(COINBASE_MATURITY + 10)
        wait_for_mature_coinbase(wallet)

        pre_balance = wallet.z_gettotalbalance(1, True)
        pre_shielded = Decimal(pre_balance['private'])

        result = wallet.z_shieldcoinbase("*", zaddr, None)
        opid = result['opid']
        assert_true(result['shieldingUTXOs'] > 0,
                    "Expected wildcard shielding to select UTXOs")
        txid = wait_and_assert_operationid_status(wallet, opid)
        assert_true(txid is not None, "Wildcard shielding should have succeeded")
        print("  Wildcard shielding tx: {}".format(txid))

        node.generate(1)
        wait_for_mature_coinbase(wallet)

        post_balance = wallet.z_gettotalbalance(1, True)
        post_shielded = Decimal(post_balance['private'])
        print("  Shielded balance: {} -> {} ZEC".format(pre_shielded, post_shielded))
        assert_true(post_shielded > pre_shielded, "Shielded balance should increase after wildcard shielding")
        print("  PASSED")

        print("Test 9: Operation lifecycle...")
        node.generate(COINBASE_MATURITY + 10)
        wait_for_mature_coinbase(wallet)

        result = wallet.z_shieldcoinbase("*", zaddr, None)
        opid = result['opid']
        assert_true(opid.startswith("opid-"), "Expected opid format")

        # z_getoperationstatus shows the operation without consuming it.
        status_list = wallet.z_getoperationstatus([opid])
        assert_equal(len(status_list), 1)
        assert_equal(status_list[0]['id'], opid)
        assert_true(
            status_list[0]['status'] in ['queued', 'executing', 'success', 'failed'],
            "Unexpected status: {}".format(status_list[0]['status'])
        )

        # z_getoperationresult waits for completion and consumes the result.
        result = wait_and_assert_operationid_status_result(wallet, opid)
        assert_equal(result['status'], 'success')
        assert_true('txid' in result['result'], "Success result should contain txid")

        # After z_getoperationresult, the operation should be cleared.
        remaining = wallet.z_getoperationstatus([opid])
        assert_equal(len(remaining), 0)
        print("  PASSED")

        print("Test 10: AllowRevealedSenders privacy policy...")
        node.generate(COINBASE_MATURITY + 10)
        wait_for_mature_coinbase(wallet)

        result = wallet.z_shieldcoinbase("*", zaddr, None, None, None, "AllowRevealedSenders")
        opid = result['opid']
        txid = wait_and_assert_operationid_status(wallet, opid)
        assert_true(txid is not None, "Shielding with AllowRevealedSenders should succeed")

        node.generate(1)
        wait_for_mature_coinbase(wallet)
        print("  PASSED")

        print("\nAll z_shieldcoinbase tests passed!")


if __name__ == '__main__':
    WalletZShieldCoinbaseTest().main()
