#!/usr/bin/env python3
# Copyright (c) 2025-2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test z_sendfromaccount RPC against the Z3 stack (zebrad + zaino + zallet).
#
# z_sendfromaccount sends funds from a single account, restricting the inputs
# to a caller-named fund source. Unlike z_sendmany it is a ONE-SHOT operation:
# it returns the resulting transaction id(s) directly (no opid / async-op
# polling), and unlike z_proposetransaction the caller must explicitly
# acknowledge the privacy implications by supplying a privacy_policy.
#
#   z_sendfromaccount(account, fund_source, recipients, minconf?, privacy_policy)
#
#     account        : the UUID of the account to spend from.
#     fund_source    : where funds may be drawn from. One of the strings
#                      "orchard", "sapling", "any_transparent" (see FundSource),
#                      or a JSON array of transparent address strings.
#     recipients     : array of { address, amount, memo? } objects. Must be
#                      non-empty, with no duplicate recipient addresses.
#     minconf        : optional; only use funds with at least this many
#                      confirmations.
#     privacy_policy : REQUIRED string naming the acceptable information
#                      leakage (see PrivacyPolicy).
#
# The response is a SendResult, serialized as { "txid": "<hex>" } when the
# operation produces a single transaction.
#
# Coverage:
#   * Validation (fast, no funding): recipients, account, every fund_source
#     error, and the required privacy_policy, matched to the backend's exact
#     messages.
#   * Functional (with mature coinbase) with exact per-account balances via
#     z_getbalances, exercising EVERY fund_source type with both a success and
#     a failure path:
#       - Orchard: single send, multi-recipient send, memo propagation, and a
#         too-high minconf (failure).
#       - Sapling: rejected while the account has no Sapling notes (failure),
#         then a successful Sapling-sourced send once the pool is funded.
#       - any_transparent: refused under FullPrivacy (failure), then a
#         successful transparent -> transparent send under AllowFullyTransparent.
#       - transparent-address array: a successful send naming the funded
#         address, and a failure naming an address the account has no funds at.
#     A transparent fund source can only pay transparent recipients, and
#     coinbase is only spendable via z_shieldcoinbase, so the transparent tests
#     spend non-coinbase UTXOs created by de-shielding to the account's taddrs.
#

from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs
from test_framework.util import (
    COINBASE_MATURITY,
    MIN_CONFIRMATIONS,
    FundSource,
    Pool,
    PrivacyPolicy,
    RpcProxy,
    account_balance_zat,
    assert_equal,
    assert_in_message,
    assert_true,
    expect_rpc_error,
    start_nodes,
    wait_and_assert_operationid_status,
    wait_for_account_balance,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_stable_mature_coinbase,
    wait_for_stable_transparent,
    wait_for_tx_scanned,
    zat,
)

# A well-formed but never-used account UUID, for negative tests.
BOGUS_ACCOUNT_UUID = "00000000-0000-0000-0000-000000000001"


# A memo is a fixed 512-byte field, so its hex encoding is 1024 characters.
MEMO_HEX_LEN = 1024


def memo_hex(text: str) -> str:
    """Encode `text` as a zallet memo: its hex, zero-padded to the 512-byte field."""
    return text.encode("utf-8").hex().ljust(MEMO_HEX_LEN, '0')


def assert_txid(result: dict) -> str:
    """Assert a SendResult carries a non-empty txid and return it."""
    assert_true(isinstance(result, dict),
                "Expected dict SendResult, got {}: {!r}".format(type(result), result))
    txid = result.get('txid')
    assert_true(isinstance(txid, str) and len(txid) > 0,
                "Expected a non-empty txid, got: {!r}".format(result))
    return txid


class WalletZSendFromAccountTest(BitcoinTestFramework):

    def __init__(self) -> None:
        super().__init__()
        # 1 node + 1 wallet. The source account and the (in-wallet) recipient
        # accounts all live in this wallet, so balance movements are observable
        # locally without a second node.
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        # All later NUs must also be listed at height 1; otherwise zebra mines a
        # coinbase committing to NU5's consensus branch ID while zallet's network
        # params expect the latest NU's branch ID, and zallet rejects the
        # coinbase on the first block sync. See ZebraArgs default in
        # test_framework/config.py.
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights={"NU5": 1, "NU6": 1, "NU6.1": 1, "NU6.2": 1},
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    # ------------------------------------------------------------------
    # Validation tests: fast (no coinbase maturity required).
    # ------------------------------------------------------------------

    def run_validation_tests(self, w0: RpcProxy, account_uuid: str, recipient: str) -> None:
        # A minimal valid recipients array, reused wherever the test exercises
        # an argument checked AFTER recipients parsing.
        good = [{"address": recipient, "amount": "0.1"}]
        policy = PrivacyPolicy.ALLOW_REVEALED_AMOUNTS

        # ---- recipients validation (checked first) ------------------

        print("Test V1: empty recipients array -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, FundSource.ORCHARD, [], 1, policy)
        assert_in_message(e, "amounts array is empty")
        print("  PASSED")

        print("Test V2: recipient address is gibberish -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, FundSource.ORCHARD,
            [{"address": "not_a_real_address", "amount": "0.1"}], MIN_CONFIRMATIONS, policy)
        assert_in_message(e, "unknown address format")
        print("  PASSED")

        print("Test V3: duplicated recipient address -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, FundSource.ORCHARD,
            [{"address": recipient, "amount": "0.1"},
             {"address": recipient, "amount": "0.2"}], MIN_CONFIRMATIONS, policy)
        assert_in_message(e, "duplicated recipient address")
        print("  PASSED")

        # ---- account validation (checked after recipients) ----------

        print("Test V4: account is a well-formed but unknown UUID...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, BOGUS_ACCOUNT_UUID, FundSource.ORCHARD, good, 1, policy)
        # A well-formed but unknown UUID is reported by get_account.
        assert_in_message(e, "No account with UUID")
        print("  PASSED")

        print("Test V5: account string is neither a UUID nor an address...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, "definitely-not-a-uuid", FundSource.ORCHARD, good, 1, policy)
        assert_true(len(e.error['message']) > 0,
                    "Expected a non-empty error for a malformed account argument")
        print("  PASSED (error: {})".format(e.error['message'][:80]))

        # ---- fund_source validation (checked after account) ---------
        #
        # These all use the real `account_uuid` so the call reaches
        # FundSource::parse rather than failing earlier on the account.

        print("Test V6: fund_source is an unknown keyword -> InvalidParameter...")
        # A deliberately-invalid keyword (not a FundSource member).
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, "transparent", good, 1, policy)
        assert_in_message(
            e,
            'Invalid fund_source: expected "orchard", "sapling", '
            '"any_transparent", or an array of transparent addresses, '
            'got "transparent".')
        print("  PASSED")

        print("Test V7: fund_source is an empty array -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, [], good, 1, policy)
        assert_in_message(
            e, "Invalid fund_source: the array of transparent addresses is empty.")
        print("  PASSED")

        print("Test V8: fund_source array contains a shielded address...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, [recipient], good, 1, policy)
        assert_in_message(e, "is not a transparent address")
        print("  PASSED")

        print("Test V9: fund_source array contains a garbage entry...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, ["not-an-address"], good, 1, policy)
        assert_in_message(
            e, 'Invalid fund_source: "not-an-address" is not a transparent address.')
        print("  PASSED")

        print("Test V10: fund_source is neither a string nor an array...")
        for bad_source in (42, True, {"pool": Pool.ORCHARD}):
            e = expect_rpc_error(
                w0.z_sendfromaccount, account_uuid, bad_source, good, 1, policy)
            assert_in_message(
                e,
                "Invalid fund_source: expected a string or an array of "
                "transparent addresses.")
        print("  PASSED")

        # ---- privacy_policy validation (checked after fund_source) --
        #
        # Valid recipients + account + fund_source, so the only thing left to
        # fail is the privacy_policy parse.

        print("Test V11: privacy_policy is an unknown value -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, FundSource.ORCHARD, good, 1, "Whatever")
        assert_in_message(e, "Unknown privacy policy Whatever")
        print("  PASSED")

        print("Test V12: privacy_policy 'LegacyCompat' is rejected...")
        # LegacyCompat is not a valid PrivacyPolicy; it is rejected explicitly.
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, FundSource.ORCHARD, good, 1, "LegacyCompat")
        assert_in_message(e, "LegacyCompat privacy policy is unsupported in Zallet")
        print("  PASSED")

        print("Test V13: every documented privacy_policy parses...")
        # The known policy names must all parse. We send from an account with no
        # Orchard notes yet, so the call fails DOWNSTREAM (proposal / build), and
        # the resulting error must NOT mention privacy_policy -- proving the
        # parse step accepted the value.
        for known in PrivacyPolicy:
            e = expect_rpc_error(
                w0.z_sendfromaccount, account_uuid, FundSource.ORCHARD, good, 1, known)
            msg = e.error['message']
            assert_true(
                "privacy policy" not in msg.lower(),
                "Valid policy {!r} unexpectedly produced a policy-parse error: "
                "{!r}".format(known, msg))
        print("  PASSED")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _transparent_receiver(wallet: RpcProxy, ua: str) -> str:
        """Return the transparent (P2PKH/P2SH) receiver of a UA built with one."""
        receivers = wallet.z_listunifiedreceivers(ua)
        if 'p2pkh' in receivers:
            return receivers['p2pkh']
        if 'p2sh' in receivers:
            return receivers['p2sh']
        raise AssertionError(
            "UA has no transparent receiver: {!r} -> {!r}".format(ua, receivers))

    def _shield_two_coinbase(self, node, w0: RpcProxy, dest_ua: str, taddr: str) -> None:
        """Shield two mature coinbase UTXOs from `taddr` into `dest_ua` and wait
        for the resulting note to be scanned."""
        wait_for_stable_mature_coinbase(w0, min_count=2)
        shield = w0.z_shieldcoinbase(taddr, dest_ua, None, 2)
        assert_equal(shield['shieldingUTXOs'], 2)
        shield_txid = wait_and_assert_operationid_status(w0, shield['opid'])
        node.generate(1)
        wait_for_tx_scanned(w0, shield_txid)

    # ------------------------------------------------------------------
    # Functional tests, grouped by fund_source type.
    # ------------------------------------------------------------------

    def run_orchard_tests(self, node, w0: RpcProxy, account_uuid: str,
                          src_orchard, extra1, extra2, r1, r2):
        # ---- FO1: single Orchard -> Orchard send, exact balances ----

        print("Test FO1: fund_source=orchard credits the recipient exactly...")
        wait_for_account_spendable(w0, account_uuid, Pool.ORCHARD, zat("0.6"))
        pre_r1 = account_balance_zat(w0, extra1, Pool.ORCHARD)
        result = w0.z_sendfromaccount(
            account_uuid, FundSource.ORCHARD,
            [{"address": r1, "amount": "0.5"}], MIN_CONFIRMATIONS, PrivacyPolicy.FULL_PRIVACY)
        txid = assert_txid(result)
        node.generate(1)
        tx = wait_for_tx_scanned(w0, txid)
        fee = zat(tx['fee'])
        wait_for_account_balance(w0, extra1, Pool.ORCHARD, pre_r1 + zat("0.5"))
        post_src = account_balance_zat(w0, account_uuid, Pool.ORCHARD)
        assert_equal(post_src, src_orchard - zat("0.5") - fee)
        src_orchard = post_src
        print("  PASSED (recipient +0.5 ZEC, source -0.5-fee, fee {} zat)".format(fee))

        # ---- FO2: multiple Orchard recipients in one transaction ----

        print("Test FO2: fund_source=orchard to two recipients, exact balances...")
        wait_for_account_spendable(w0, account_uuid, Pool.ORCHARD, zat("0.25"))
        pre_r1 = account_balance_zat(w0, extra1, Pool.ORCHARD)
        pre_r2 = account_balance_zat(w0, extra2, Pool.ORCHARD)
        result = w0.z_sendfromaccount(
            account_uuid, FundSource.ORCHARD,
            [{"address": r1, "amount": "0.1"},
             {"address": r2, "amount": "0.1"}],
            MIN_CONFIRMATIONS, PrivacyPolicy.FULL_PRIVACY)
        txid = assert_txid(result)
        node.generate(1)
        tx = wait_for_tx_scanned(w0, txid)
        fee = zat(tx['fee'])
        wait_for_account_balance(w0, extra1, Pool.ORCHARD, pre_r1 + zat("0.1"))
        wait_for_account_balance(w0, extra2, Pool.ORCHARD, pre_r2 + zat("0.1"))
        post_src = account_balance_zat(w0, account_uuid, Pool.ORCHARD)
        assert_equal(post_src, src_orchard - zat("0.2") - fee)
        print("  PASSED (both recipients +0.1 ZEC, fee {} zat)".format(fee))

        # ---- FO3: memo propagation to a shielded recipient ----------

        print("Test FO3: a memo rides on the shielded recipient output...")
        # A memo whose first byte is below 0xF5 is a UTF-8 TEXT memo (ZIP 302),
        # so its bytes must actually be valid UTF-8. Zallet does not check that
        # when sending (it takes the raw bytes), but it does when reading the
        # note back, and an unreadable memo makes z_listunspent fail for the
        # whole wallet. So send what a real caller sends: text, zero-padded to
        # the fixed 512-byte field.
        my_memo = memo_hex("zallet fund-source memo test")
        wait_for_account_spendable(w0, account_uuid, Pool.ORCHARD, zat("0.15"))
        pre_r1 = account_balance_zat(w0, extra1, Pool.ORCHARD)
        result = w0.z_sendfromaccount(
            account_uuid, FundSource.ORCHARD,
            [{"address": r1, "amount": "0.1", "memo": my_memo}],
            MIN_CONFIRMATIONS, PrivacyPolicy.FULL_PRIVACY)
        txid = assert_txid(result)
        node.generate(1)
        tx = wait_for_tx_scanned(w0, txid)
        wait_for_account_balance(w0, extra1, Pool.ORCHARD, pre_r1 + zat("0.1"))
        shielded = [o for o in tx.get('outputs', [])
                    if o.get('pool') in (Pool.SAPLING, Pool.ORCHARD)]
        matching = [o for o in shielded if o.get('memo') == my_memo]
        assert_equal(
            len(matching), 1,
            "Expected exactly one shielded output carrying our memo; got memos "
            "{} from tx {}".format([o.get('memo') for o in shielded], txid))
        print("  PASSED")

        # ---- FO4: a too-high minconf makes the funds unselectable ---

        print("Test FO4: a minconf beyond the note's confirmations fails...")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, FundSource.ORCHARD,
            [{"address": r1, "amount": "0.1"}], 1000000, PrivacyPolicy.FULL_PRIVACY)
        assert_true(len(e.error['message']) > 0,
                    "Expected a non-empty error for an unsatisfiable minconf")
        print("  PASSED (error: {})".format(e.error['message'][:80]))

    def run_sapling_tests(self, node, w0: RpcProxy, account_uuid: str, sapling_ua, extra1, r1, taddr):
        # ---- FS1: fund_source=sapling fails with no Sapling notes ---

        print("Test FS1: fund_source=sapling fails when the account has no Sapling notes...")
        # The account holds Orchard (and transparent) funds but no Sapling notes,
        # so restricting the source to Sapling must be unsatisfiable rather than
        # silently spending Orchard.
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, FundSource.SAPLING,
            [{"address": r1, "amount": "0.1"}], MIN_CONFIRMATIONS, PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        assert_true(len(e.error['message']) > 0,
                    "Expected a non-empty error when the restricted pool is empty")
        print("  PASSED (error: {})".format(e.error['message'][:80]))

        # ---- FS2: fund the Sapling pool, then spend from it ---------

        print("Test FS2: fund_source=sapling spends Sapling notes (success)...")
        self._shield_two_coinbase(node, w0, sapling_ua, taddr)
        wait_for_account_spendable(w0, account_uuid, Pool.SAPLING, zat("0.4"))
        src_sapling = account_balance_zat(w0, account_uuid, Pool.SAPLING)
        assert_true(src_sapling > 0, "shielding should have funded the Sapling pool")

        # Sapling source with an Orchard recipient (and Orchard change) is
        # cross-pool, which reveals amounts, so AllowRevealedAmounts is required.
        pre_r1 = account_balance_zat(w0, extra1, Pool.ORCHARD)
        result = w0.z_sendfromaccount(
            account_uuid, FundSource.SAPLING,
            [{"address": r1, "amount": "0.3"}], MIN_CONFIRMATIONS, PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        txid = assert_txid(result)
        node.generate(1)
        wait_for_tx_scanned(w0, txid)
        wait_for_account_balance(w0, extra1, Pool.ORCHARD, pre_r1 + zat("0.3"))
        assert_true(
            account_balance_zat(w0, account_uuid, Pool.SAPLING) < src_sapling,
            "the Sapling balance should drop after a Sapling-sourced send")
        print("  PASSED (recipient +0.3 ZEC, Sapling source spent)")

    def run_transparent_tests(self, node, w0: RpcProxy, account_uuid: str, orchard_ua,
                              own_taddr_a, own_taddr_b, unfunded_taddr,
                              extra1, extra2, r1_taddr, r2_taddr, taddr):
        # IMPORTANT PROPERTY: coinbase outputs can only be spent via
        # z_shieldcoinbase, never selected by a transfer's input selection. So
        # transparent fund sources spend only NON-coinbase transparent funds.
        # Create two such UTXOs by shielding coinbase to Orchard, then
        # de-shielding to the account's own transparent addresses.
        #
        # IMPORTANT PROPERTY: a transparent fund source can only pay TRANSPARENT
        # recipients. A single-step proposal cannot carry an ephemeral shielded
        # change output, so transparent -> shielded is rejected with "a
        # transparent fund source supports only transparent recipients". These
        # tests therefore send transparent -> transparent (change returns to the
        # account as an ordinary transparent output).
        self._shield_two_coinbase(node, w0, orchard_ua, taddr)
        wait_for_account_spendable(w0, account_uuid, Pool.ORCHARD, zat("4.1"))
        deshield = w0.z_sendfromaccount(
            account_uuid, FundSource.ORCHARD,
            [{"address": own_taddr_a, "amount": "2.0"},
             {"address": own_taddr_b, "amount": "2.0"}],
            MIN_CONFIRMATIONS, PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        node.generate(1)
        wait_for_tx_scanned(w0, assert_txid(deshield))
        # Gate on z_listunspent (the signal the proposal builder's transparent
        # input selection tracks).
        wait_for_stable_transparent(w0, min_count=2, minconf=1)

        # ---- FT1: privacy enforcement on a transparent source ------

        print("Test FT1: any_transparent under FullPrivacy is refused...")
        # A fully-transparent send reveals senders and recipients, so the strict
        # FullPrivacy policy must refuse it (no funds move).
        pre_r1 = account_balance_zat(w0, extra1, Pool.TRANSPARENT)
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, FundSource.ANY_TRANSPARENT,
            [{"address": r1_taddr, "amount": "1.0"}], MIN_CONFIRMATIONS, PrivacyPolicy.FULL_PRIVACY)
        assert_true(len(e.error['message']) > 0,
                    "Expected a privacy-policy rejection for a transparent source")
        assert_equal(account_balance_zat(w0, extra1, Pool.TRANSPARENT), pre_r1)
        print("  PASSED (error: {})".format(e.error['message'][:80]))

        # ---- FA1: explicit single-address array, exact recipient ---

        print("Test FA1: fund_source=[<taddr>] -> transparent credits the recipient...")
        # Restricting to own_taddr_b spends that UTXO and leaves own_taddr_a's
        # for FT2, proving the array restricts to the listed address.
        pre_r1 = account_balance_zat(w0, extra1, Pool.TRANSPARENT)
        result = w0.z_sendfromaccount(
            account_uuid, [own_taddr_b],
            [{"address": r1_taddr, "amount": "1.0"}], MIN_CONFIRMATIONS,
            PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
        txid = assert_txid(result)
        node.generate(1)
        wait_for_tx_scanned(w0, txid)
        wait_for_account_balance(w0, extra1, Pool.TRANSPARENT, pre_r1 + zat("1.0"))
        print("  PASSED (recipient +1.0 ZEC)")

        # ---- FT2: any_transparent -> transparent, exact recipient --

        print("Test FT2: any_transparent -> transparent credits the recipient exactly...")
        # Spends the remaining (own_taddr_a) UTXO.
        wait_for_stable_transparent(w0, min_count=1, minconf=1)
        pre_r2 = account_balance_zat(w0, extra2, Pool.TRANSPARENT)
        result = w0.z_sendfromaccount(
            account_uuid, FundSource.ANY_TRANSPARENT,
            [{"address": r2_taddr, "amount": "1.0"}], MIN_CONFIRMATIONS,
            PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
        txid = assert_txid(result)
        node.generate(1)
        wait_for_tx_scanned(w0, txid)
        wait_for_account_balance(w0, extra2, Pool.TRANSPARENT, pre_r2 + zat("1.0"))
        print("  PASSED (recipient +1.0 ZEC)")

        # ---- FA2: address array naming an address with no funds ----

        print("Test FA2: fund_source=[<address with no account funds>] fails...")
        # The account has no funds at `unfunded_taddr`, so restricting the source
        # to it leaves nothing to spend.
        assert_true(unfunded_taddr not in (own_taddr_a, own_taddr_b, taddr),
                    "unfunded taddr collides with a funded address")
        e = expect_rpc_error(
            w0.z_sendfromaccount, account_uuid, [unfunded_taddr],
            [{"address": r1_taddr, "amount": "0.1"}], MIN_CONFIRMATIONS,
            PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
        assert_true(len(e.error['message']) > 0,
                    "Expected an insufficient-funds error for an unfunded address")
        print("  PASSED (error: {})".format(e.error['message'][:80]))

    def run_test(self) -> None:
        node = self.nodes[0]
        w0 = self.wallets[0]
        taddr = self.miner_addresses[0]

        # Account RPCs reject until the wallet has committed at least one block
        # (they need a chain height to anchor a new account's birthday).
        self.sync_all()

        # Identify account 0 (the wallet's default account).
        accounts = w0.z_listaccounts()
        assert_true(len(accounts) >= 1, "Wallet 0 should have at least one account")
        account_uuid = accounts[0]['account_uuid']

        # Orchard-only and Sapling-only UAs on account 0, the shielding
        # destinations that give the account spendable notes in each pool.
        orchard_ua = w0.z_getaddressforaccount(account_uuid, ["orchard"])['address']
        sapling_ua = w0.z_getaddressforaccount(account_uuid, ["sapling"])['address']

        # Two recipient accounts in this wallet, observable via z_getbalances.
        # Each gets a shielded (Orchard) UA for the shielded-source tests and a
        # transparent receiver for the transparent-source tests (a transparent
        # fund source can only pay transparent recipients).
        extra1 = w0.z_getnewaccount("recipient1")['account_uuid']
        extra2 = w0.z_getnewaccount("recipient2")['account_uuid']
        r1 = w0.z_getaddressforaccount(extra1, ["orchard"])['address']
        r2 = w0.z_getaddressforaccount(extra2, ["orchard"])['address']
        r1_taddr = self._transparent_receiver(
            w0, w0.z_getaddressforaccount(extra1, ["orchard", "p2pkh"])['address'])
        r2_taddr = self._transparent_receiver(
            w0, w0.z_getaddressforaccount(extra2, ["orchard", "p2pkh"])['address'])

        # Two transparent addresses owned by account 0 (to hold non-coinbase
        # UTXOs for the transparent fund-source tests), and one owned by extra2
        # that account 0 has no funds at (for the array-restriction test).
        own_taddr_a = self._transparent_receiver(
            w0, w0.z_getaddressforaccount(account_uuid, ["orchard", "p2pkh"])['address'])
        own_taddr_b = self._transparent_receiver(
            w0, w0.z_getaddressforaccount(account_uuid, ["orchard", "p2pkh"])['address'])
        assert_true(own_taddr_a != own_taddr_b, "expected distinct own taddrs")
        unfunded_taddr = self._transparent_receiver(
            w0, w0.z_getaddressforaccount(extra2, ["orchard", "p2pkh"])['address'])

        print("Mining initial blocks to mature coinbase...")
        node.generate(COINBASE_MATURITY + 20)
        expected_mature = node.getblockcount() - COINBASE_MATURITY + 1
        wait_for_mature_coinbase_count(w0, expected_mature)
        print("  Mature coinbase UTXOs: {}".format(expected_mature))
        print("  Account 0 UUID:    {}".format(account_uuid))
        print("  Recipient 1 UA:    {}...".format(r1[:24]))
        print("  Recipient 2 UA:    {}...".format(r2[:24]))

        print("\n==== Validation tests ====")
        self.run_validation_tests(w0, account_uuid, r1)

        print("\n==== Funding the source account's Orchard pool ====")
        self._shield_two_coinbase(node, w0, orchard_ua, taddr)
        src_orchard = account_balance_zat(w0, account_uuid, Pool.ORCHARD)
        assert_true(src_orchard > 0, "shielding should have funded the Orchard pool")
        print("  Account 0 Orchard balance: {} zat".format(src_orchard))

        print("\n==== Functional tests (Orchard source) ====")
        self.run_orchard_tests(
            node, w0, account_uuid, src_orchard, extra1, extra2, r1, r2)

        print("\n==== Functional tests (Sapling source) ====")
        self.run_sapling_tests(node, w0, account_uuid, sapling_ua, extra1, r1, taddr)

        print("\n==== Functional tests (transparent source) ====")
        self.run_transparent_tests(
            node, w0, account_uuid, orchard_ua, own_taddr_a, own_taddr_b,
            unfunded_taddr, extra1, extra2, r1_taddr, r2_taddr, taddr)

        print("\nAll z_sendfromaccount tests passed!")


if __name__ == '__main__':
    WalletZSendFromAccountTest().main()
