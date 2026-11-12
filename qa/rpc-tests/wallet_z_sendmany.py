#!/usr/bin/env python3
# Copyright (c) 2020-2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test z_sendmany RPC against the Z3 stack (zebrad + zaino + zallet).
#
# z_sendmany sends funds from a single source address to one or more
# recipients as a background async operation:
#
#   z_sendmany(fromaddress, amounts, minconf?, fee?, privacy_policy?)
#
#     fromaddress    : a wallet-owned address (Unified, Sapling, or
#                      transparent) whose account's funds are spent.
#     amounts        : array of { address, amount, memo? } objects. Must be
#                      non-empty, with no duplicate recipient addresses.
#     minconf        : optional; only use funds with at least this many
#                      confirmations.
#     fee            : optional; MUST be null. Zallet always computes the
#                      ZIP-317 fee internally. Present for positional
#                      compatibility with zcashd only.
#     privacy_policy : optional; defaults to FullPrivacy. Names the acceptable
#                      information leakage; a send that would leak more than
#                      the policy permits fails.
#
# z_sendmany returns an opid; the resulting transaction id is obtained via the
# operation-status RPCs.
#
# This is the Z3-stack rewrite of the historical zcashd z_sendmany test. The
# original leaned on RPCs zallet does not implement (z_exportviewingkey,
# sendtoaddress, getwalletinfo shielded-balance fields, sapling z_getnewaddress)
# and on a pre-NU5 regtest chain; the Z3 regtest activates NU5+ from height 1.
# We therefore preserve the parts that remain meaningful: argument validation,
# a shielded happy path, multi-recipient sends, and the privacy-policy gating
# that is the heart of z_sendmany. Because zallet still maps some backend
# errors to a non-zcashd shape ("TODO: Map errors to zcashd shape"), the
# privacy-gating tests assert the success/failure outcome rather than pinning
# exact zcashd wording; the synchronous parse errors, whose messages are
# stable, are asserted exactly.
#
# TODO: extend once zallet can spend transparent/coinbase (and reliably Sapling)
# sources in z_sendmany. That unblocks the privacy policies not covered here:
# AllowRevealedSenders (transparent -> shielded), AllowRevealedAmounts (cross-pool),
# AllowLinkingAccountAddresses, and NoPrivacy overrides. The same limitation keeps
# the sibling policy tests disabled (wallet_sendmany_any_taddr, wallet_unified_change).
#

from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs
from test_framework.util import (
    COINBASE_MATURITY,
    Pool,
    PrivacyPolicy,
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
    wait_for_tx_scanned,
    zat,
)


class WalletZSendmanyTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        # 1 node + 1 wallet. The source account and the (in-wallet) recipient
        # accounts all live in this wallet, so balance movements are observable
        # locally without a second node.
        self.num_nodes = 1
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
                activation_heights={"NU5": 1, "NU6": 1, "NU6.1": 1, "NU6.2": 1},
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    # ------------------------------------------------------------------
    # Validation: synchronous argument errors (no funding required).
    #
    # These are rejected before z_sendmany starts a background operation, so
    # the call raises a JSONRPCException directly rather than returning an
    # opid. Their messages are stable, so we assert them exactly.
    # ------------------------------------------------------------------

    def run_validation_tests(self, w0, source, recipient):
        good = [{"address": recipient, "amount": "0.1"}]

        print("Test V1: empty amounts array -> InvalidParameter...")
        e = expect_rpc_error(w0.z_sendmany, source, [], 1, None, PrivacyPolicy.FULL_PRIVACY)
        assert_in_message(e, "amounts array is empty")
        print("  PASSED")

        print("Test V2: recipient address is gibberish -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_sendmany, source,
            [{"address": "not_a_real_address", "amount": "0.1"}],
            1, None, PrivacyPolicy.FULL_PRIVACY)
        assert_in_message(e, "unknown address format")
        print("  PASSED")

        print("Test V3: duplicated recipient address -> InvalidParameter...")
        e = expect_rpc_error(
            w0.z_sendmany, source,
            [{"address": recipient, "amount": "0.1"},
             {"address": recipient, "amount": "0.2"}],
            1, None, PrivacyPolicy.FULL_PRIVACY)
        assert_in_message(e, "duplicated recipient address")
        print("  PASSED")

        print("Test V4: a non-null fee is rejected...")
        # `fee` exists only for positional zcashd compatibility; zallet always
        # computes the fee internally and rejects any non-null value.
        for bad_fee in (0, 1000, "0.0001"):
            e = expect_rpc_error(w0.z_sendmany, source, good, 1, bad_fee, PrivacyPolicy.FULL_PRIVACY)
            assert_in_message(e, "fee field must be null")
        print("  PASSED")

        print("Test V5: an unknown privacy_policy is rejected...")
        e = expect_rpc_error(w0.z_sendmany, source, good, 1, None, "ZcashIsAwesome")
        assert_in_message(e, "Unknown privacy policy ZcashIsAwesome")
        print("  PASSED")

        print("Test V6: the 'LegacyCompat' privacy_policy is rejected...")
        e = expect_rpc_error(w0.z_sendmany, source, good, 1, None, "LegacyCompat")
        assert_in_message(e, "LegacyCompat privacy policy is unsupported in Zallet")
        print("  PASSED")

    # ------------------------------------------------------------------
    # Functional: require funded Orchard notes.
    # ------------------------------------------------------------------

    def run_functional_tests(self, node, w0, account_uuid, source,
                             extra1, extra2, recipient_orchard,
                             recipient_orchard_2, src_orchard, taddr):
        # ---- F1: shielded Orchard -> Orchard send under FullPrivacy --

        print("Test F1: z_sendmany Orchard -> Orchard, exact per-account balances...")
        # Omitting privacy_policy defaults to FullPrivacy; an Orchard->Orchard
        # send with Orchard change reveals nothing, so it must succeed.
        wait_for_account_spendable(w0, account_uuid, Pool.ORCHARD, zat("0.6"))
        pre_r1 = account_balance_zat(w0, extra1, Pool.ORCHARD)
        opid = w0.z_sendmany(source, [{"address": recipient_orchard, "amount": "0.5"}], 1, None)
        txid = wait_and_assert_operationid_status(w0, opid)
        assert_true(txid is not None, "send should produce a txid")
        print("  send txid: {}".format(txid))

        node.generate(1)
        tx_details = wait_for_tx_scanned(w0, txid)
        fee = zat(tx_details['fee'])
        # Recipient is credited the exact amount; source is debited amount + fee
        # (change returns to Orchard).
        wait_for_account_balance(w0, extra1, Pool.ORCHARD, pre_r1 + zat("0.5"))
        post_src = account_balance_zat(w0, account_uuid, Pool.ORCHARD)
        assert_equal(post_src, src_orchard - zat("0.5") - fee)
        src_orchard = post_src
        shielded_outputs = [o for o in tx_details.get('outputs', [])
                            if o.get('pool') in (Pool.SAPLING, Pool.ORCHARD)]
        assert_true(len(shielded_outputs) >= 1,
                    "Expected at least one shielded output, got: {!r}".format(
                        tx_details.get('outputs')))
        print("  PASSED (recipient +0.5 ZEC, source -0.5-fee, fee {} zat)".format(fee))

        # ---- F2: multiple shielded recipients in one transaction ----

        print("Test F2: z_sendmany to multiple Orchard recipients, exact balances...")
        wait_for_account_spendable(w0, account_uuid, Pool.ORCHARD, zat("0.25"))
        pre_r1 = account_balance_zat(w0, extra1, Pool.ORCHARD)
        pre_r2 = account_balance_zat(w0, extra2, Pool.ORCHARD)
        opid = w0.z_sendmany(
            source,
            [{"address": recipient_orchard, "amount": "0.1"},
             {"address": recipient_orchard_2, "amount": "0.1"}],
            1, None)
        txid = wait_and_assert_operationid_status(w0, opid)
        node.generate(1)
        tx_details = wait_for_tx_scanned(w0, txid)
        fee = zat(tx_details['fee'])
        wait_for_account_balance(w0, extra1, Pool.ORCHARD, pre_r1 + zat("0.1"))
        wait_for_account_balance(w0, extra2, Pool.ORCHARD, pre_r2 + zat("0.1"))
        post_src = account_balance_zat(w0, account_uuid, Pool.ORCHARD)
        assert_equal(post_src, src_orchard - zat("0.2") - fee)
        print("  PASSED (both recipients +0.1 ZEC, fee {} zat)".format(fee))

        # ---- F3: privacy gating on a transparent recipient ----------

        print("Test F3: revealing a transparent recipient requires a weaker policy...")
        # Sending shielded funds to a bare transparent recipient publicly
        # reveals the recipient and amount. z_sendmany validates the privacy
        # policy against the recipients synchronously, so the default
        # (FullPrivacy) is rejected with a JSONRPCException before any opid.
        wait_for_account_spendable(w0, account_uuid, Pool.ORCHARD, zat("0.15"))
        recipients = [{"address": taddr, "amount": "0.1"}]
        e = expect_rpc_error(w0.z_sendmany, source, recipients, 1, None)
        assert_in_message(e, "transparent recipient")
        print("  default policy correctly refused the transparent recipient")

        # With a policy that permits revealed recipients, the same send
        # succeeds (and is processed as a background operation).
        opid = w0.z_sendmany(source, recipients, 1, None, PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        txid = wait_and_assert_operationid_status(w0, opid)
        assert_true(txid is not None, "send with AllowRevealedRecipients should succeed")
        node.generate(1)
        wait_for_tx_scanned(w0, txid)
        print("  PASSED")

        # ---- F4: a memo to a transparent recipient is rejected ------

        print("Test F4: a memo addressed to a transparent recipient fails...")
        # Memos can only ride on shielded outputs; attaching one to a transparent
        # recipient is rejected synchronously when the recipients are parsed.
        e = expect_rpc_error(
            w0.z_sendmany, source,
            [{"address": taddr, "amount": "0.1", "memo": "DEADBEEF"}],
            1, None, PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        msg = e.error['message']
        assert_true('memo' in msg.lower() or 'transparent' in msg.lower(),
                    "Expected a memo/transparent error, got: {!r}".format(msg))
        print("  PASSED")

    def run_test(self):
        node = self.nodes[0]
        w0 = self.wallets[0]
        taddr = self.miner_addresses[0]

        # Account RPCs reject until the wallet has committed at least one block
        # (they need a chain height to anchor a new account's birthday).
        self.sync_all()

        accounts = w0.z_listaccounts()
        assert_true(len(accounts) >= 1, "Wallet 0 should have at least one account")
        account_uuid = accounts[0]['account_uuid']

        # An Orchard-only UA on account 0, used both as the shielding
        # destination (so the account holds spendable Orchard notes) and as the
        # z_sendmany `fromaddress`.
        source = w0.z_getaddressforaccount(account_uuid, ["orchard"])['address']

        # Two recipient accounts in this same wallet, so payments to them are
        # observable in the wallet-level private balance.
        extra1 = w0.z_getnewaccount("recipient1")['account_uuid']
        extra2 = w0.z_getnewaccount("recipient2")['account_uuid']
        recipient_orchard = w0.z_getaddressforaccount(extra1, ["orchard"])['address']
        recipient_orchard_2 = w0.z_getaddressforaccount(extra2, ["orchard"])['address']

        print("Mining initial blocks to mature coinbase...")
        node.generate(COINBASE_MATURITY + 20)
        expected_mature = node.getblockcount() - COINBASE_MATURITY + 1
        wait_for_mature_coinbase_count(w0, expected_mature)
        print("  Mature coinbase UTXOs: {}".format(expected_mature))
        print("  Account 0 UUID:    {}".format(account_uuid))
        print("  Source Orchard UA: {}...".format(source[:24]))

        print("\n==== Validation tests ====")
        self.run_validation_tests(w0, source, recipient_orchard)

        # Fund the source account's Orchard pool by shielding a couple of mature
        # coinbase UTXOs into its Orchard-only UA. Keep the count small to avoid
        # the pure-Orchard many-UTXO fee-estimation path (see wallet.py note).
        print("\nFunding the source account's Orchard pool...")
        shield = w0.z_shieldcoinbase(taddr, source, None, 2)
        assert_equal(shield['shieldingUTXOs'], 2)
        shield_txid = wait_and_assert_operationid_status(w0, shield['opid'])
        node.generate(1)
        wait_for_tx_scanned(w0, shield_txid)
        src_orchard = account_balance_zat(w0, account_uuid, Pool.ORCHARD)
        assert_true(src_orchard > 0, "shielding should have funded the Orchard pool")

        print("\n==== Functional tests ====")
        self.run_functional_tests(
            node, w0, account_uuid, source, extra1, extra2,
            recipient_orchard, recipient_orchard_2, src_orchard, taddr)

        print("\nAll z_sendmany tests passed!")


if __name__ == '__main__':
    WalletZSendmanyTest().main()
