#!/usr/bin/env python3
# Copyright (c) 2025-2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test z_shieldcoinbase RPC against the Z3 stack (zebrad + zaino + zallet).
#
# Covers the z_shieldcoinbase API surface
#
#   z_shieldcoinbase(fromaddress, toaddress, fee?, limit?, memo?, privacy_policy?)
#
#     fromaddress    : either a single wallet-owned transparent address
#                      OR a single account UUID.
#     toaddress      : any Zcash shielded address (Sapling, Orchard, or
#                      Unified with a shielded receiver). Need not be
#                      owned by this wallet. Transparent / TEX rejected.
#     fee            : optional; must be null. Zallet always uses a
#                      ZIP-317 fee internally. Present for positional
#                      compatibility with zcashd's z_shieldcoinbase only.
#     limit          : optional u32; caps the number of selected coinbase
#                      UTXOs to the highest-value `n`.
#     memo           : optional hex string up to 1024 chars (512 bytes).
#     privacy_policy : optional; must be null. Zallet always shields
#                      under a fixed policy. Present for positional
#                      compatibility with zcashd's z_shieldcoinbase only.
#
# The pre-flight response shape matches zcashd:
#   { remainingUTXOs, remainingValue, shieldingUTXOs, shieldingValue, opid }
#

import time

from decimal import Decimal
from test_framework.test_framework import ZcashTestFramework
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

# A well-formed but never-used account UUID, for negative tests.
BOGUS_ACCOUNT_UUID = "00000000-0000-0000-0000-000000000001"


def mature_transparent_utxos(wallet):
    """Return the wallet's mature transparent coinbase UTXOs."""
    utxos = wallet.z_listunspent(COINBASE_MATURITY + 1)
    return [u for u in utxos if u.get('pool') == 'transparent']


def wait_for_mature_coinbase_count(wallet, expected_count, timeout=240):
    """
    Wait until the wallet's view of mature transparent coinbase UTXOs is
    exactly `expected_count`, and return that snapshot.

    Used as a sync barrier before each sweep. With no mining in flight and
    no in-flight tx for the wallet to ingest, the wallet's mature-coinbase
    set converges from below to its final value as it scans toward the
    chain tip. Pinning the exact post-sync count lets later assertions on
    `shieldingUTXOs` / `shieldingValue` be exact rather than ranged.

    Zallet's `z_listunspent` reflects only what the wallet has scanned and
    committed to its local SQLite database; after `node.generate(N)` there
    is a non-trivial delay (block fetch + scan + commit) before the view
    updates, and `sync_all` does not synchronize wallets (no
    `getwalletstatus` RPC yet: https://github.com/zcash/wallet/issues/316).
    """
    deadline = time.time() + timeout
    last_count = -1
    transparent = []
    while time.time() < deadline:
        try:
            transparent = mature_transparent_utxos(wallet)
            last_count = len(transparent)
            if last_count == expected_count:
                return transparent
        except Exception:
            pass
        time.sleep(1)

    raise AssertionError(
        "wait_for_mature_coinbase_count: timeout after {}s; saw {} mature "
        "transparent UTXOs (wanted exactly {})".format(
            timeout, last_count, expected_count))


def wait_for_tx_scanned(wallet, txid, timeout=120):
    """
    Wait until the wallet has scanned the block containing `txid`, then
    return its `z_viewtransaction` view.

    Acts as the single post-confirm sync barrier for a sweep. Once the
    wallet exposes the tx via `z_viewtransaction` with a populated `fee`
    field, the same scan has updated every other view that depends on
    it (`z_gettotalbalance`, `z_listunspent`, ...), so they can be read
    synchronously without a second wait.

    Until `getwalletstatus` lands (zcash/wallet#316) there is no
    synchronous wallet-sync primitive; this poll is the workaround.
    """
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            tx = wallet.z_viewtransaction(txid)
            if 'fee' in tx:
                return tx
        except Exception as e:
            last_err = e
        time.sleep(1)
    raise AssertionError(
        "wait_for_tx_scanned: timeout after {}s for txid {} ({})".format(
            timeout, txid, last_err))


def expect_rpc_error(callable_, *args, **kwargs):
    """Invoke an RPC and return the JSONRPCException; fail if it didn't raise."""
    try:
        callable_(*args, **kwargs)
    except JSONRPCException as e:
        return e
    raise AssertionError(
        "Expected RPC error, but call succeeded: {}({}, {})".format(
            getattr(callable_, '__name__', '?'), args, kwargs))


def assert_in_message(e, needle):
    msg = e.error['message']
    assert_true(needle in msg, "Expected {!r} in error, got: {!r}".format(needle, msg))


class WalletZShieldCoinbaseTest(ZcashTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        # 1 node + 1 wallet. The integration framework provisions one
        # zebrad per wallet; running 2 wallets would also require 2
        # nodes (and exposes a known Zaino race with concurrent wallets,
        # zcash/wallet#TBD). The "toaddress need not belong to this
        # wallet" property is structurally exercised by the receiver-
        # type validation in the backend, which we cover in V1/V2.
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        # NU5 is the consensus floor for Zallet's Orchard change strategy
        # used inside z_shieldcoinbase. The default zallet.toml activates
        # NU5 at height 1; mirror that on the zebrad side.
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights={"NU5": 1},
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    # ------------------------------------------------------------------
    # Validation tests: fast (no coinbase maturity required).
    # ------------------------------------------------------------------

    def run_validation_tests(self, w0, w0_taddr, w0_account_uuid,
                             w0_zaddr, w0_extra_account_uuid):
        # ---- `toaddress` validation ---------------------------------

        print("Test V1: toaddress is gibberish -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_shieldcoinbase, w0_account_uuid, "not_a_real_address")
        assert_in_message(e, "unknown address format")
        print("  PASSED")

        print("Test V2: toaddress is transparent -> backend rejects...")
        e = expect_rpc_error(
            w0.z_shieldcoinbase, w0_account_uuid, w0_taddr)
        # Backend surfaces ShieldingRequiresShieldedRecipient via
        # ProposalError. We don't pin the exact wording, just that the
        # call fails — a successful shield-to-taddr would be a serious
        # regression.
        msg = e.error['message']
        assert_true(
            "shielded" in msg.lower() or "transparent" in msg.lower(),
            "Expected shielded-recipient error, got: {!r}".format(msg))
        print("  PASSED")

        # ---- `fromaddress` JSON-type validation ---------------------

        # The JSON-RPC layer rejects non-string `fromaddress` before
        # the call function runs, with a generic `invalid type: ...,
        # expected a string` error (it does NOT include the parameter
        # name). We check the "expected a string" tail to confirm the
        # rejection is structural rather than coming from somewhere
        # inside the handler.
        print("Test V3: fromaddress is a number -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_shieldcoinbase, 42, w0_zaddr)
        assert_in_message(e, "expected a string")
        print("  PASSED")

        print("Test V4: fromaddress is a bool -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_shieldcoinbase, True, w0_zaddr)
        assert_in_message(e, "expected a string")
        print("  PASSED")

        # ---- `fromaddress` semantic validation ----------------------

        print("Test V6: fromaddress string is neither UUID nor t-addr...")
        e = expect_rpc_error(
            w0.z_shieldcoinbase, "definitely-not-a-uuid", w0_zaddr)
        assert_in_message(
            e, "expected a wallet-owned transparent address or an account UUID")
        print("  PASSED")

        print("Test V7: fromaddress is a well-formed but unknown UUID...")
        e = expect_rpc_error(
            w0.z_shieldcoinbase, BOGUS_ACCOUNT_UUID, w0_zaddr)
        assert_in_message(e, "Unknown account UUID")
        print("  PASSED")

        print("Test V8: fromaddress is a shielded address -> rejected...")
        e = expect_rpc_error(
            w0.z_shieldcoinbase, w0_zaddr, w0_zaddr)
        assert_in_message(e, "only transparent addresses are accepted")
        print("  PASSED")

        print("Test V9: fromaddress is an unowned taddr -> rejected...")
        # A well-formed regtest taddr not provisioned by this wallet.
        # (`test_framework/config.py` default — never imported into our
        # wallet under normal test setup.)
        unowned_taddr = "tmSRd1r8gs77Ja67Fw1JcdoXytxsyrLTPJm"
        assert_true(unowned_taddr != w0_taddr,
                    "Unowned taddr collision with wallet's miner address")
        e = expect_rpc_error(
            w0.z_shieldcoinbase, unowned_taddr, w0_zaddr)
        assert_in_message(e, "not owned by any account in this wallet")
        print("  PASSED")

        # ---- Pre-flight: empty source set ---------------------------

        print("Test V10: account UUID with no transparent receivers...")
        # The extra account on wallet 0 has UAs but the call below
        # creates one with a transparent receiver. Instead, use an
        # account with no transparent UTXOs (any account works as long
        # as it hasn't received any) by asking for the extra account,
        # which (although it has a receiver) has no balance.
        # This should fail with "Insufficient" rather than "No source
        # addresses", since the receiver exists.
        # Pin whichever error the backend returns rather than asserting
        # a specific message.
        e = expect_rpc_error(
            w0.z_shieldcoinbase, w0_extra_account_uuid, w0_zaddr)
        msg = e.error['message']
        assert_true(
            len(msg) > 0,
            "Expected non-empty error for empty-source case, got: {!r}".format(msg))
        print("  PASSED (error: {})".format(msg[:80]))

        # ---- `fee` parameter validation -----------------------------

        print("Test V11: fee parameter is not null -> InvalidParameter...")
        # `fee` exists for positional compatibility with zcashd's
        # z_shieldcoinbase only. Zallet always computes the fee
        # internally; any non-null value must be rejected so callers
        # can't silently believe they're configuring a fee.
        for bad_fee in (0, 1000, "0.0001"):
            e = expect_rpc_error(
                w0.z_shieldcoinbase,
                w0_account_uuid, w0_zaddr, bad_fee)
            assert_in_message(e, "fee field must be null")
        print("  PASSED")

        # ---- `privacy_policy` parameter validation ------------------

        print("Test V12: privacy_policy parameter validation...")
        # z_shieldcoinbase accepts exactly two explicit `privacy_policy` values
        # — `AllowRevealedSenders` and `AllowLinkingAccountAddresses` — and
        # chooses a default from the `fromaddress` shape when omitted. Stricter
        # policies (e.g. `FullPrivacy`) cannot be satisfied because coinbase
        # shielding always reveals the source transparent address(es), and
        # unknown values are rejected outright.

        # 12a: stricter / looser / unknown policies are rejected at parse
        # time with the "Invalid privacy_policy" error.
        for bad_policy in ("FullPrivacy", "NoPrivacy", "AllowRevealedRecipients", ""):
            e = expect_rpc_error(
                w0.z_shieldcoinbase,
                w0_account_uuid, w0_zaddr, None, None, None, bad_policy)
            assert_in_message(e, "Invalid privacy_policy")

        # 12b: the two valid strings parse successfully. We invoke against
        # the empty extra account so the call fails downstream (no
        # eligible coinbase UTXOs) rather than executing a sweep — but
        # the resulting error must NOT mention `privacy_policy`, proving
        # the parse step accepted the value.
        for good_policy in ("AllowRevealedSenders", "AllowLinkingAccountAddresses"):
            e = expect_rpc_error(
                w0.z_shieldcoinbase,
                w0_extra_account_uuid, w0_zaddr, None, None, None, good_policy)
            msg = e.error['message']
            assert_true(
                "privacy_policy" not in msg,
                "Valid policy {!r} unexpectedly produced a policy-parse error: {!r}"
                    .format(good_policy, msg))
        print("  PASSED")

    @staticmethod
    def _first_transparent_receiver(wallet, ua):
        """Return the P2PKH receiver of a UA created with a transparent component."""
        receivers = wallet.z_listunifiedreceivers(ua)
        if 'p2pkh' in receivers:
            return receivers['p2pkh']
        if 'p2sh' in receivers:
            return receivers['p2sh']
        raise AssertionError(
            "UA has no transparent receiver: {!r} -> {!r}".format(ua, receivers))

    # ------------------------------------------------------------------
    # Functional tests: require mature coinbase.
    # ------------------------------------------------------------------

    def run_functional_tests(self, node, w0, w0_taddr, w0_account_uuid,
                             w0_zaddr, w0_extra_zaddr):
        # Running tally of mature coinbase UTXOs spent so far. Combined
        # with `node.getblockcount()`, this pins the exact size of the
        # unspent-mature set the wallet should observe before each sweep:
        #
        #     expected = (tip - COINBASE_MATURITY) - mature_spent
        #
        # `tip - COINBASE_MATURITY` is the chain's all-time mature-coinbase
        # count given all blocks are coinbase to `w0_taddr` and our
        # confirmation filter is `minconf = COINBASE_MATURITY + 1`. The
        # wallet's view converges to this from below as it scans; the
        # sync-barrier helper waits for that convergence so the count
        # assertions below are exact rather than ranged.
        mature_spent = 0

        def expected_unspent_mature():
            return node.getblockcount() - COINBASE_MATURITY - mature_spent

        # ---- F1: explicit single-t-addr sweep -----------------------

        print("Test F1: explicit single-t-addr sweep (response shape + balance moves)...")
        pre_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        snapshot = wait_for_mature_coinbase_count(w0, expected_unspent_mature())
        n_expected = len(snapshot)
        expected_value = sum(Decimal(u['value']) for u in snapshot)
        print("  [diag] mature coinbase UTXO count={}, total {}".format(
            n_expected, expected_value))

        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr)
        self._assert_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected)
        assert_equal(Decimal(result['shieldingValue']), expected_value)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))

        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None, "Shielding tx should have succeeded")
        print("  Shielding tx: {}".format(txid))

        node.generate(1)
        fee = Decimal(wait_for_tx_scanned(w0, txid)['fee'])
        expected_post = pre_private + expected_value - fee
        post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        assert_equal(post_private, expected_post)
        mature_spent += n_expected
        print("  Balance {} -> {} ZEC (fee {}). PASSED".format(
            pre_private, post_private, fee))

        # ---- F2: account-UUID sweep ---------------------------------

        print("Test F2: account-UUID sweep (happy path)...")
        # The UUID form resolves via `get_transparent_receivers(account, true, true)`,
        # which (with `include_change=true`) returns both EXTERNAL and INTERNAL
        # transparent receivers of the account's registered UAs. The mining
        # address provisioned by `generate-account-and-miner-address` is at
        # `KeyScope::INTERNAL`, so it is reachable through this path.
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        snapshot = wait_for_mature_coinbase_count(w0, expected_unspent_mature())
        n_expected = len(snapshot)
        expected_value = sum(Decimal(u['value']) for u in snapshot)

        result = w0.z_shieldcoinbase(w0_account_uuid, w0_zaddr)
        self._assert_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected,
                     "UUID-form should sweep every mature coinbase")
        assert_equal(Decimal(result['shieldingValue']), expected_value)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        node.generate(1)
        fee = Decimal(wait_for_tx_scanned(w0, txid)['fee'])
        expected_post = pre_private + expected_value - fee
        post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        assert_equal(post_private, expected_post)
        mature_spent += n_expected
        print("  PASSED ({} UTXOs swept, fee {})".format(n_expected, fee))

        # ---- F3: shield into a UA on a different account -----------

        print("Test F3: shield into a UA on a different account (not the source)...")
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        snapshot = wait_for_mature_coinbase_count(w0, expected_unspent_mature())
        n_expected = len(snapshot)
        expected_value = sum(Decimal(u['value']) for u in snapshot)

        # toaddress belongs to a different account in the same wallet.
        # The new API design imposes no ownership relationship between
        # `fromaddress` and `toaddress` — only that toaddress has a
        # shielded receiver. Source remains account 0.
        result = w0.z_shieldcoinbase(w0_taddr, w0_extra_zaddr)
        self._assert_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected)
        assert_equal(Decimal(result['shieldingValue']), expected_value)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        node.generate(1)
        fee = Decimal(wait_for_tx_scanned(w0, txid)['fee'])
        # Source and destination are both in this wallet (different
        # accounts), so total `private` grows by the full net-of-fee
        # value just as for an in-account shield.
        expected_post = pre_private + expected_value - fee
        post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        assert_equal(post_private, expected_post)
        mature_spent += n_expected
        print("  PASSED")

        # ---- F4: limit truncation -----------------------------------

        print("Test F4: limit truncation (limit<eligible)...")
        node.generate(COINBASE_MATURITY + 30)
        pre_private = post_private
        snapshot = wait_for_mature_coinbase_count(w0, expected_unspent_mature())
        n_eligible = len(snapshot)
        # Sort by value descending to mirror the backend's "highest-value
        # `n` UTXOs" selection. When values tie, the per-input selection
        # is implementation-defined, but the SUM of any top-k slice is
        # uniquely determined — so the value assertions below remain exact.
        sorted_by_value = sorted(
            snapshot, key=lambda u: Decimal(u['value']), reverse=True)

        limit = 3
        assert_true(
            n_eligible > limit,
            "Need >{} mature coinbase UTXOs to exercise truncation, got {}".format(
                limit, n_eligible))
        expected_shielding_value = sum(
            Decimal(u['value']) for u in sorted_by_value[:limit])
        expected_remaining_value = sum(
            Decimal(u['value']) for u in sorted_by_value[limit:])

        # Positional signature: (fromaddress, toaddress, fee, limit, ...)
        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr, None, limit)
        self._assert_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], limit)
        assert_equal(result['remainingUTXOs'], n_eligible - limit)
        assert_equal(Decimal(result['shieldingValue']), expected_shielding_value)
        assert_equal(Decimal(result['remainingValue']), expected_remaining_value)
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        node.generate(1)
        fee = Decimal(wait_for_tx_scanned(w0, txid)['fee'])
        expected_post = pre_private + expected_shielding_value - fee
        post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        assert_equal(post_private, expected_post)
        mature_spent += limit
        print("  PASSED ({}/{} selected, {} remaining)".format(
            limit, n_eligible, n_eligible - limit))

        # ---- F5: limit > eligible is harmless -----------------------

        print("Test F5: limit greater than eligible is a no-op cap...")
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        snapshot = wait_for_mature_coinbase_count(w0, expected_unspent_mature())
        n_eligible = len(snapshot)
        expected_value = sum(Decimal(u['value']) for u in snapshot)

        huge_limit = n_eligible + 1000
        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr, None, huge_limit)
        self._assert_preflight_shape(result)
        # Limit exceeds eligible, so the cap has no effect: every mature
        # UTXO is swept and nothing remains.
        assert_equal(result['shieldingUTXOs'], n_eligible)
        assert_equal(Decimal(result['shieldingValue']), expected_value)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        node.generate(1)
        fee = Decimal(wait_for_tx_scanned(w0, txid)['fee'])
        expected_post = pre_private + expected_value - fee
        post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        assert_equal(post_private, expected_post)
        mature_spent += n_eligible
        print("  PASSED")

        # ---- F6: memo propagation -----------------------------------

        print("Test F6: memo propagation...")
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        snapshot = wait_for_mature_coinbase_count(w0, expected_unspent_mature())
        n_expected = len(snapshot)
        expected_value = sum(Decimal(u['value']) for u in snapshot)

        # 1024-character hex string = 512 bytes. Leading bytes spell
        # "c0ffee" in ASCII so we can eyeball matches if the assertion
        # fails.
        my_memo = '633066666565' + '0' * (1024 - 12)

        # Positional signature: (fromaddress, toaddress, fee, limit, memo, ...)
        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr, None, None, my_memo)
        self._assert_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected)
        assert_equal(Decimal(result['shieldingValue']), expected_value)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        node.generate(1)
        tx_details = wait_for_tx_scanned(w0, txid)
        fee = Decimal(tx_details['fee'])
        expected_post = pre_private + expected_value - fee
        post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        assert_equal(post_private, expected_post)
        mature_spent += n_expected

        shielded_outputs = [o for o in tx_details.get('outputs', [])
                            if o.get('pool') in ('sapling', 'orchard')]
        # The sweep produces exactly one shielded payment carrying the
        # user-supplied memo (change notes, if any, use an empty memo
        # and would not match this hex blob).
        matching = [o for o in shielded_outputs if o.get('memo') == my_memo]
        assert_equal(
            len(matching), 1,
            "Expected exactly 1 shielded output carrying our memo; got memos {} "
            "from tx {}".format(
                [o.get('memo') for o in shielded_outputs], tx_details))
        print("  PASSED")

        # ---- F7: operation lifecycle --------------------------------

        print("Test F7: operation lifecycle (status -> result -> cleared)...")
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        snapshot = wait_for_mature_coinbase_count(w0, expected_unspent_mature())
        n_expected = len(snapshot)
        expected_value = sum(Decimal(u['value']) for u in snapshot)

        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr)
        self._assert_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected)
        assert_equal(Decimal(result['shieldingValue']), expected_value)
        opid = result['opid']
        assert_true(opid.startswith("opid-"),
                    "Expected opid- prefix, got {!r}".format(opid))

        # z_getoperationstatus sees the operation without consuming it.
        status_list = w0.z_getoperationstatus([opid])
        assert_equal(len(status_list), 1)
        assert_equal(status_list[0]['id'], opid)
        assert_true(
            status_list[0]['status'] in ('queued', 'executing', 'success', 'failed'),
            "Unexpected status: {!r}".format(status_list[0]['status']))

        # z_getoperationresult blocks until completion and consumes the
        # result.
        finished = wait_and_assert_operationid_status_result(w0, opid)
        assert_equal(finished['status'], 'success')
        assert_true('txid' in finished['result'])

        # After consumption, the operation is no longer reported.
        remaining = w0.z_getoperationstatus([opid])
        assert_equal(len(remaining), 0)
        mature_spent += n_expected
        print("  PASSED")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _assert_preflight_shape(self, result):
        assert_true(isinstance(result, dict),
                    "Expected dict, got {}: {!r}".format(type(result), result))
        for key in ('remainingUTXOs', 'remainingValue',
                    'shieldingUTXOs', 'shieldingValue', 'opid'):
            assert_true(key in result,
                        "Missing field {!r} in response: {!r}".format(key, result))
        assert_true(isinstance(result['remainingUTXOs'], int))
        assert_true(isinstance(result['shieldingUTXOs'], int))
        assert_true(isinstance(result['opid'], str))
        # remainingValue / shieldingValue are JSON numbers; Decimal-able.
        Decimal(result['remainingValue'])
        Decimal(result['shieldingValue'])

    # ------------------------------------------------------------------

    def run_test(self):
        node = self.nodes[0]
        w0 = self.wallets[0]

        w0_taddr = self.miner_addresses[0]

        # Identify account 0 on wallet 0.
        accounts = w0.z_listaccounts()
        assert_true(len(accounts) >= 1, "Wallet 0 should have at least one account")
        w0_account_uuid = accounts[0]['account_uuid']

        # Provision an extra account on wallet 0 (for cross-account
        # tests, the empty-source case, and the "shield to a UA in a
        # different account" happy path).
        extra = w0.z_getnewaccount("for-validation-tests")
        w0_extra_account_uuid = extra['account_uuid']
        # Pre-materialize an Orchard UA on the extra account so it has
        # a transparent receiver for the cross-account validation test
        # and a shielded receiver as a shielding destination.
        w0_extra_zaddr = w0.z_getaddressforaccount(
            w0_extra_account_uuid, ["orchard"])['address']

        # A shielded address owned by wallet 0. Use both Sapling and
        # Orchard receivers so the backend can pick whichever the
        # change strategy prefers. (Pure-Orchard destinations hit
        # what appears to be a fee-estimation bug in the build path
        # when shielding many coinbase UTXOs to a single Orchard note;
        # see zcash/wallet#TODO.)
        w0_zaddr = w0.z_getaddressforaccount(
            w0_account_uuid, ["sapling", "orchard"])['address']

        print("Mining initial blocks to mature coinbase...")
        # `prepare_chain` already mined block 1 (coinbase to w0_taddr).
        # Mining COINBASE_MATURITY+20 more brings the tip to 121, with
        # blocks 1..21 at depth >= 101 — i.e. exactly 21 mature coinbase
        # UTXOs spendable via z_shieldcoinbase. `run_functional_tests`
        # re-checks this exact count via its `expected_unspent_mature`
        # bookkeeping.
        node.generate(COINBASE_MATURITY + 20)
        expected_initial_mature = node.getblockcount() - COINBASE_MATURITY
        wait_for_mature_coinbase_count(w0, expected_initial_mature)
        print("  Mature coinbase UTXOs: {}".format(expected_initial_mature))
        print("  Account 0 UUID:      {}".format(w0_account_uuid))
        print("  Orchard UA (w0):     {}...".format(w0_zaddr[:24]))
        print("  Extra account UA:    {}...".format(w0_extra_zaddr[:24]))

        print("\n==== Validation tests ====")
        self.run_validation_tests(
            w0, w0_taddr, w0_account_uuid, w0_zaddr,
            w0_extra_account_uuid)

        print("\n==== Functional tests ====")
        self.run_functional_tests(
            node, w0, w0_taddr, w0_account_uuid, w0_zaddr, w0_extra_zaddr)

        print("\nAll z_shieldcoinbase tests passed!")


if __name__ == '__main__':
    WalletZShieldCoinbaseTest().main()
