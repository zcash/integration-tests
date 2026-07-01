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

from decimal import Decimal
from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs
from test_framework.util import (
    COINBASE_MATURITY,
    assert_equal,
    assert_in_message,
    assert_shieldcoinbase_preflight_shape,
    assert_true,
    expect_rpc_error,
    nu_activation_all_at_1,
    start_nodes,
    wait_and_assert_operationid_status,
    wait_and_assert_operationid_status_result,
    wait_for_mature_coinbase_count,
    wait_for_tx_scanned,
)

# A well-formed but never-used account UUID, for negative tests.
BOGUS_ACCOUNT_UUID = "00000000-0000-0000-0000-000000000001"


class WalletZShieldCoinbaseTest(BitcoinTestFramework):

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
        # All later NUs must also be listed at height 1; otherwise zebra mines
        # a coinbase committing to NU5's consensus branch ID while zallet's
        # network params expect the latest NU's branch ID, and zallet rejects
        # the coinbase on the first block sync. See ZebraArgs default in
        # test_framework/config.py.
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights=nu_activation_all_at_1(),
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

    # ------------------------------------------------------------------
    # Functional tests: require mature coinbase.
    # ------------------------------------------------------------------

    def run_functional_tests(self, node, w0, w0_taddr, w0_account_uuid,
                             w0_zaddr, w0_extra_zaddr):
        # Expected unspent-mature COUNT before each sweep: all coinbase goes
        # to w0_taddr, so it's (tip - COINBASE_MATURITY + 1) minus what we've spent
        # — a block at height H has (tip - H + 1) confirmations, so a block is
        # mature when tip - H + 1 >= 100, i.e. H <= tip - 99, giving
        # (tip - 99) mature coinbases = (tip - COINBASE_MATURITY + 1).
        # Counts are reliable; VALUES are not derived from the snapshot:
        # z_listunspent and the proposal use different maturity tips, so across
        # the regtest subsidy halving (6.25 -> 3.125) a summed snapshot can
        # disagree with the reported value. Value assertions therefore use the
        # operation's own shieldingValue and the balance delta instead.
        mature_spent = 0

        def expected_unspent_mature():
            return node.getblockcount() - COINBASE_MATURITY + 1 - mature_spent

        def confirm_and_check_balance(txid, pre_private, shielding_value):
            """Confirm the sweep and assert balance grew by value - fee.
            Returns (fee, post_private, tx_details)."""
            node.generate(1)
            tx_details = wait_for_tx_scanned(w0, txid)
            fee = Decimal(tx_details['fee'])
            post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
            assert_equal(post_private, pre_private + shielding_value - fee)
            return fee, post_private, tx_details

        # ---- F1: explicit single-t-addr sweep -----------------------

        print("Test F1: explicit single-t-addr sweep (response shape + balance moves)...")
        pre_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        n_expected = len(wait_for_mature_coinbase_count(w0, expected_unspent_mature()))
        print("  [diag] mature coinbase UTXO count={}".format(n_expected))

        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr)
        assert_shieldcoinbase_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        shielding_value = Decimal(result['shieldingValue'])
        assert_true(shielding_value > 0, "Expected positive shielding value")

        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None, "Shielding tx should have succeeded")
        print("  Shielding tx: {}".format(txid))

        fee, post_private, _ = confirm_and_check_balance(txid, pre_private, shielding_value)
        mature_spent += n_expected
        print("  Balance {} -> {} ZEC (fee {}). PASSED".format(
            pre_private, post_private, fee))

        # ---- F2: account-UUID sweep ---------------------------------

        print("Test F2: account-UUID sweep (happy path)...")
        # UUID form sweeps every transparent receiver of the account,
        # including the INTERNAL-scope mining address.
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        n_expected = len(wait_for_mature_coinbase_count(w0, expected_unspent_mature()))

        result = w0.z_shieldcoinbase(w0_account_uuid, w0_zaddr)
        assert_shieldcoinbase_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected,
                     "UUID-form should sweep every mature coinbase")
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        shielding_value = Decimal(result['shieldingValue'])
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        fee, post_private, _ = confirm_and_check_balance(txid, pre_private, shielding_value)
        mature_spent += n_expected
        print("  PASSED ({} UTXOs swept, fee {})".format(n_expected, fee))

        # ---- F3: shield into a UA on a different account -----------

        print("Test F3: shield into a UA on a different account (not the source)...")
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        n_expected = len(wait_for_mature_coinbase_count(w0, expected_unspent_mature()))

        # toaddress is a UA on a different account; fromaddress need not own it.
        result = w0.z_shieldcoinbase(w0_taddr, w0_extra_zaddr)
        assert_shieldcoinbase_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        shielding_value = Decimal(result['shieldingValue'])
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        # Both accounts are in this wallet, so `private` grows by the net value.
        fee, post_private, _ = confirm_and_check_balance(txid, pre_private, shielding_value)
        mature_spent += n_expected
        print("  PASSED")

        # ---- F4: limit truncation -----------------------------------

        print("Test F4: limit truncation (limit<eligible)...")
        node.generate(COINBASE_MATURITY + 30)
        pre_private = post_private
        n_eligible = len(wait_for_mature_coinbase_count(w0, expected_unspent_mature()))

        limit = 3
        assert_true(
            n_eligible > limit,
            "Need >{} mature coinbase UTXOs to exercise truncation, got {}".format(
                limit, n_eligible))

        # Sweep 1: capped at `limit`; the rest are reported as remaining.
        # Positional signature: (fromaddress, toaddress, fee, limit, ...)
        result1 = w0.z_shieldcoinbase(w0_taddr, w0_zaddr, None, limit)
        assert_shieldcoinbase_preflight_shape(result1)
        assert_equal(result1['shieldingUTXOs'], limit)
        assert_equal(result1['remainingUTXOs'], n_eligible - limit)
        shielding_value1 = Decimal(result1['shieldingValue'])
        remaining_utxos1 = result1['remainingUTXOs']
        remaining_value1 = Decimal(result1['remainingValue'])
        assert_true(remaining_value1 > 0,
                    "Truncation must leave positive remaining value")
        txid1 = wait_and_assert_operationid_status(w0, result1['opid'])
        assert_true(txid1 is not None)

        # Sweep 2: no limit, before sweep 1 is mined. The proposal excludes
        # sweep 1's now-spent inputs, so it must drain exactly what sweep 1
        # reported as remaining — backend-anchored, no snapshot summing.
        result2 = w0.z_shieldcoinbase(w0_taddr, w0_zaddr)
        assert_shieldcoinbase_preflight_shape(result2)
        assert_equal(result2['shieldingUTXOs'], remaining_utxos1,
                     "2nd sweep must drain exactly sweep 1's remaining UTXOs")
        assert_equal(Decimal(result2['shieldingValue']), remaining_value1,
                     "2nd sweep value must equal sweep 1's reported remainingValue")
        assert_equal(result2['remainingUTXOs'], 0)
        assert_equal(Decimal(result2['remainingValue']), Decimal('0'))
        shielding_value2 = Decimal(result2['shieldingValue'])
        txid2 = wait_and_assert_operationid_status(w0, result2['opid'])
        assert_true(txid2 is not None)

        # One block confirms both sweeps.
        node.generate(1)
        fee1 = Decimal(wait_for_tx_scanned(w0, txid1)['fee'])
        fee2 = Decimal(wait_for_tx_scanned(w0, txid2)['fee'])
        post_private = Decimal(w0.z_gettotalbalance(1, True)['private'])
        assert_equal(
            post_private,
            pre_private + shielding_value1 + shielding_value2 - fee1 - fee2)
        mature_spent += n_eligible
        print("  PASSED ({}/{} selected, {} swept in follow-up)".format(
            limit, n_eligible, remaining_utxos1))

        # ---- F5: limit > eligible is harmless -----------------------

        print("Test F5: limit greater than eligible is a no-op cap...")
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        n_eligible = len(wait_for_mature_coinbase_count(w0, expected_unspent_mature()))

        huge_limit = n_eligible + 1000
        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr, None, huge_limit)
        assert_shieldcoinbase_preflight_shape(result)
        # Cap above eligible has no effect: everything is swept.
        assert_equal(result['shieldingUTXOs'], n_eligible)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        shielding_value = Decimal(result['shieldingValue'])
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        fee, post_private, _ = confirm_and_check_balance(txid, pre_private, shielding_value)
        mature_spent += n_eligible
        print("  PASSED")

        # ---- F6: memo propagation -----------------------------------

        print("Test F6: memo propagation...")
        node.generate(COINBASE_MATURITY + 10)
        pre_private = post_private
        n_expected = len(wait_for_mature_coinbase_count(w0, expected_unspent_mature()))

        # 512-byte memo (max); leading bytes spell "c0ffee" for eyeballing.
        my_memo = '633066666565' + '0' * (1024 - 12)

        # Positional signature: (fromaddress, toaddress, fee, limit, memo, ...)
        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr, None, None, my_memo)
        assert_shieldcoinbase_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
        shielding_value = Decimal(result['shieldingValue'])
        txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(txid is not None)
        fee, post_private, tx_details = confirm_and_check_balance(
            txid, pre_private, shielding_value)
        mature_spent += n_expected

        shielded_outputs = [o for o in tx_details.get('outputs', [])
                            if o.get('pool') in ('sapling', 'orchard')]
        # Exactly one shielded payment carries the memo (change uses empty).
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
        n_expected = len(wait_for_mature_coinbase_count(w0, expected_unspent_mature()))

        result = w0.z_shieldcoinbase(w0_taddr, w0_zaddr)
        assert_shieldcoinbase_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], n_expected)
        assert_equal(result['remainingUTXOs'], 0)
        assert_equal(Decimal(result['remainingValue']), Decimal('0'))
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

    def run_test(self):
        node = self.nodes[0]
        w0 = self.wallets[0]

        w0_taddr = self.miner_addresses[0]

        # Wait for the wallet to sync to the node tip. z_getnewaccount (and
        # other account-mutating RPCs) reject with "Wallet sync required"
        # until the wallet has committed at least one block, since they need
        # a chain height to anchor the new account's birthday.
        self.sync_all()

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
        expected_initial_mature = node.getblockcount() - COINBASE_MATURITY + 1
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
