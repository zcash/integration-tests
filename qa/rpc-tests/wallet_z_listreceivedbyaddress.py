#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test z_listreceivedbyaddress RPC against the Z3 stack (zebrad + zaino + zallet).
#
# Verifies that z_listreceivedbyaddress returns ALL outputs (spent and
# unspent) received by an address: account-level matching (including internal
# change) for a shielded/unified address, and taddr-only matching for a
# transparent address. Also covers minconf semantics, the unmined-entry field
# shape, ordering, asOfHeight, pagination, and the RPC's error cases.
#

from decimal import Decimal
from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs
from test_framework.util import (
    COINBASE_MATURITY,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    PrivacyPolicy,
    TotalBalanceField,
    assert_equal,
    assert_in_message,
    assert_true,
    expect_rpc_error,
    nu_activation_all_at_1,
    start_nodes,
    wait_and_assert_operationid_status,
    wait_for_total_balance,
    wait_for_tx_scanned,
)

# The memo text sent in the self-send used to exercise unmined-entry fields
# and memo/memoStr decoding.
SELF_SEND_MEMO = "hello alice"

# The memo-bearing self-send amount (case 2).
SELF_SEND_AMOUNT = Decimal('1.0')

# The amount alice sends to bob in case 3, chosen to leave alice with change
# but still spend "most" of her balance so the spend is unambiguous.
SPEND_TO_BOB_AMOUNT = Decimal('7.0')

# The transparent amount bob sends to alice's second UA's p2pkh receiver in
# case 4.
TRANSPARENT_SEND_AMOUNT = Decimal('0.5')

# z_listreceivedbyaddress's `asOfHeight` sentinel meaning "no bound / the
# chain tip", i.e. its default. Named so a call that must also fill in a
# later positional argument (offset/limit) doesn't carry a bare `-1`.
NO_AS_OF_HEIGHT_BOUND = -1

# z_listreceivedbyaddress's `offset` default (start from the first entry).
# Named for the same reason as NO_AS_OF_HEIGHT_BOUND: calls that must fill in
# `limit` positionally still need to spell out the default before it.
DEFAULT_OFFSET = 0

# A deliberately invalid `limit`, used in case 5e to exercise the RPC's
# "limit must be positive" rejection.
ZERO_LIMIT = 0

# The page size used by case 7's pagination sweep.
PAGE_SIZE = 1


class WalletZListReceivedByAddressTest(BitcoinTestFramework):
    """
    Test the z_listreceivedbyaddress RPC method.

    Uses 2 nodes and 2 wallets:
      - wallet 0 (alice): receives and spends shielded/transparent funds
      - wallet 1 (bob):   sends to alice, and is used for the "wrong wallet"
                           negative case
    """

    def __init__(self):
        super().__init__()
        self.num_nodes = 2
        self.num_wallets = 2
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights=nu_activation_all_at_1(),
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def run_test(self):
        node = self.nodes[0]
        alice = self.wallets[0]
        bob = self.wallets[1]

        self.sync_all()

        # ================================================================
        # Case 1: setup/funding
        # ================================================================
        print("Case 1: funding alice's UA via shielded coinbase...")
        alice_accounts = alice.z_listaccounts()
        assert_true(len(alice_accounts) >= 1, "Alice should have at least one account")
        alice_account_uuid = alice_accounts[0]['account_uuid']

        # A UA with both shielded receivers, so shielded funds can land in
        # either pool depending on what the sender's wallet selects.
        alice_ua = alice.z_getaddressforaccount(
            alice_account_uuid, ["sapling", "orchard"])['address']

        node.generate(COINBASE_MATURITY + 10)
        self.sync_all()

        alice_taddr = self.miner_addresses[0]
        result = alice.z_shieldcoinbase(alice_taddr, alice_ua)
        txid_shield = wait_and_assert_operationid_status(alice, result['opid'])
        node.generate(1)
        self.sync_all()
        wait_for_tx_scanned(alice, txid_shield)
        print("  PASSED")

        # ================================================================
        # Case 2: minconf semantics + unmined-entry field shape
        # ================================================================
        print("Case 2: minconf semantics and unmined-entry fields...")
        recipients = [{
            "address": alice_ua,
            "amount": SELF_SEND_AMOUNT,
            "memo": SELF_SEND_MEMO.encode().hex(),
        }]
        opid = alice.z_sendmany(alice_ua, recipients, MIN_CONFIRMATIONS)
        txid_self = wait_and_assert_operationid_status(alice, opid)
        self.sync_all()

        # Before mining: minconf=1 (the default) must not surface the
        # mempool-only receive.
        r_default = alice.z_listreceivedbyaddress(alice_ua, MIN_CONFIRMATIONS)
        assert_true(
            all(e['txid'] != txid_self for e in r_default),
            "unconfirmed receive must not appear under minconf=1")

        # minconf=0 must surface it, with the "unmined" field values. The
        # self-send produces two entries for this txid under account-level
        # matching: the external receive carrying the memo, and the internal
        # change note; select the memo-carrying one.
        r_unconfirmed = alice.z_listreceivedbyaddress(alice_ua, 0)
        unmined = [e for e in r_unconfirmed if e['txid'] == txid_self]
        assert_true(len(unmined) >= 1, "unconfirmed receive must appear under minconf=0")
        entry = next(
            (e for e in unmined if e.get('memoStr') == SELF_SEND_MEMO), None)
        assert_true(
            entry is not None,
            "the memo-carrying external receive must appear under minconf=0")
        assert_equal(entry['confirmations'], 0)
        assert_equal(entry['blockheight'], 0)
        assert_equal(entry['blockindex'], -1)
        assert_equal(entry['blocktime'], 0)
        assert_equal(entry['amount'], SELF_SEND_AMOUNT)
        assert_equal(entry['amountZat'], int(SELF_SEND_AMOUNT * 10**8))
        # 'memo' is the hex of the full 512-byte memo field (the text,
        # zero-padded); decode and strip the padding to recover the text.
        decoded_memo = bytes.fromhex(entry['memo']).rstrip(b'\x00')
        assert_equal(decoded_memo, SELF_SEND_MEMO.encode())
        assert_equal(entry['memoStr'], SELF_SEND_MEMO)

        node.generate(1)
        self.sync_all()
        wait_for_tx_scanned(alice, txid_self)

        # After mining: the default call (minconf omitted) now finds it, with
        # real confirmations/blockheight/blockindex/blocktime. The external
        # receive appears alongside the self-send's change notes (how many
        # notes change is split into is a change-strategy implementation
        # detail, so only the lower bound is asserted).
        r_mined = alice.z_listreceivedbyaddress(alice_ua)
        mined = [e for e in r_mined if e['txid'] == txid_self]
        assert_true(len(mined) >= 2, "external receive and change notes must be listed")
        memo_entries = [e for e in mined if e.get('memoStr') == SELF_SEND_MEMO]
        assert_equal(len(memo_entries), 1)
        mined_external = memo_entries[0]
        assert_equal(mined_external['confirmations'], 1)
        assert_true(mined_external['blockheight'] > 0, "mined entry must report a real height")
        assert_true(mined_external['blockindex'] >= 0, "mined entry must report a real blockindex")
        assert_true(mined_external['blocktime'] > 0, "mined entry must report a real blocktime")
        self_send_height = mined_external['blockheight']
        print("  PASSED")

        # ================================================================
        # Case 3: spent notes remain listed + change entries
        # ================================================================
        print("Case 3: spent outputs remain listed, change entries present...")
        bob_accounts = bob.z_listaccounts()
        assert_true(len(bob_accounts) >= 1, "Bob should have at least one account")
        bob_account_uuid = bob_accounts[0]['account_uuid']
        bob_ua = bob.z_getaddressforaccount(bob_account_uuid, ["sapling", "orchard"])['address']

        r_before_spend = alice.z_listreceivedbyaddress(alice_ua)
        txids_before_spend = set(e['txid'] for e in r_before_spend)

        opid = alice.z_sendmany(
            alice_ua,
            [{'address': bob_ua, 'amount': SPEND_TO_BOB_AMOUNT}],
            MIN_CONFIRMATIONS)
        txid_spend = wait_and_assert_operationid_status(alice, opid)
        node.generate(1)
        self.sync_all()
        wait_for_tx_scanned(alice, txid_spend)
        # Bob spends part of these funds in case 4; wait for his wallet's
        # scan (which runs independently of alice's) to report them as
        # spendable via its total-balance summary (zcash/wallet#316).
        wait_for_total_balance(
            bob, TotalBalanceField.PRIVATE,
            lambda v: v >= TRANSPARENT_SEND_AMOUNT)

        r_after_spend = alice.z_listreceivedbyaddress(alice_ua)
        txids_after_spend = set(e['txid'] for e in r_after_spend)
        assert_true(
            txids_before_spend.issubset(txids_after_spend),
            "previously received outputs must still be listed after being spent")

        # Every entry that carries spending authority (i.e. is not from a
        # watch-only/view-only account) must report a boolean 'change' field.
        for e in r_after_spend:
            assert_true('change' in e, "spendable-account entries must have a 'change' field")
            assert_true(isinstance(e['change'], bool), "'change' must be a bool")

        assert_true(
            any(e['change'] for e in r_after_spend),
            "spending most of the balance must produce an internal change note")
        print("  PASSED")

        # ================================================================
        # Case 4: transparent per-address listing
        # ================================================================
        # Fund a second UA of alice's account that includes a p2pkh receiver,
        # and have bob pay its bare transparent receiver directly (a
        # transparent recipient under AllowRevealedRecipients). Querying that
        # taddr must return only the transparent receive, with no 'memo' key.
        print("Case 4: transparent per-address listing...")
        alice_ua2 = alice.z_getaddressforaccount(
            alice_account_uuid, ["sapling", "p2pkh"])['address']
        alice_receivers2 = alice.z_listunifiedreceivers(alice_ua2)
        alice_taddr2 = alice_receivers2['p2pkh']

        opid = bob.z_sendmany(
            bob_ua,
            [{'address': alice_taddr2, 'amount': TRANSPARENT_SEND_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        txid_taddr = wait_and_assert_operationid_status(bob, opid)
        node.generate(1)
        self.sync_all()
        wait_for_tx_scanned(alice, txid_taddr)
        # wait_for_tx_scanned tracks the shielded scan; transparent output
        # detection can lag behind it, so also wait until the receipt shows up
        # in alice's transparent balance before listing.
        wait_for_total_balance(
            alice, TotalBalanceField.TRANSPARENT,
            lambda v: v >= TRANSPARENT_SEND_AMOUNT)

        # The UA's own p2pkh receiver is a bare receiver and is rejected when
        # queried directly (case 5f); the transparent receive is surfaced by
        # the account-level (UA) listing instead. A transaction whose only
        # output of interest to this wallet is transparent is detected from
        # the mempool, and the block scan (which has nothing to trial-decrypt
        # in it) does not stamp its mined height — that happens later via
        # asynchronous transaction-status polling. Until then the wallet
        # reports the receive as unconfirmed, so query with minconf=0.
        r_ua2 = alice.z_listreceivedbyaddress(alice_ua2, 0)
        taddr_entries = [
            e for e in r_ua2
            if e['txid'] == txid_taddr and e['pool'] == 'transparent'
        ]
        assert_equal(len(taddr_entries), 1)
        assert_equal(taddr_entries[0]['amount'], TRANSPARENT_SEND_AMOUNT)
        assert_equal(taddr_entries[0]['amountZat'], int(TRANSPARENT_SEND_AMOUNT * 10**8))
        assert_true(
            'memo' not in taddr_entries[0],
            "transparent entries must not carry a memo key")

        # Per-address transparent listing: the miner address is tracked by the
        # wallet in its own right, so querying it is legal and returns only
        # transparent outputs — including the coinbase outputs that case 1's
        # z_shieldcoinbase already spent. The absence of any shielded entry
        # demonstrates per-address (not account-level) matching, since the
        # same account holds plenty of shielded receipts by now.
        r_miner = alice.z_listreceivedbyaddress(alice_taddr)
        assert_true(len(r_miner) >= 1, "the miner address's coinbase receipts must be listed")
        for e in r_miner:
            assert_equal(e['pool'], 'transparent')
            assert_true('memo' not in e, "transparent entries must not carry a memo key")
        print("  PASSED")

        # ================================================================
        # Case 5: negative cases
        # ================================================================
        print("Case 5: negative cases...")

        # (a) undecodable address
        e = expect_rpc_error(alice.z_listreceivedbyaddress, "notanaddress")
        assert_equal(e.error['code'], -5)
        assert_in_message(e, "Invalid zaddr.")
        print("  Case 5a PASSED")

        # (b) an address of another wallet
        e = expect_rpc_error(bob.z_listreceivedbyaddress, alice_ua)
        assert_equal(e.error['code'], -5)
        assert_in_message(e, "does not belong to this node")
        print("  Case 5b PASSED")

        # (c) a bare receiver (from z_listunifiedreceivers) of a multi-receiver
        # UA in this wallet
        alice_receivers = alice.z_listunifiedreceivers(alice_ua)
        alice_bare_sapling = alice_receivers['sapling']
        e = expect_rpc_error(alice.z_listreceivedbyaddress, alice_bare_sapling)
        assert_equal(e.error['code'], -8)
        assert_in_message(e, "bare receiver")
        print("  Case 5c PASSED")

        # (d) minconf=0 combined with asOfHeight
        e = expect_rpc_error(
            alice.z_listreceivedbyaddress, alice_ua, 0, self_send_height)
        assert_equal(e.error['code'], -8)
        assert_in_message(e, "Require a minimum of 1 confirmation when `asOfHeight` is provided")
        print("  Case 5d PASSED")

        # (e) limit=0
        e = expect_rpc_error(
            alice.z_listreceivedbyaddress, alice_ua, MIN_CONFIRMATIONS,
            NO_AS_OF_HEIGHT_BOUND, DEFAULT_OFFSET, ZERO_LIMIT)
        assert_equal(e.error['code'], -8)
        assert_in_message(e, "limit must be positive")
        print("  Case 5e PASSED")

        # (f) a bare p2pkh receiver of a multi-receiver UA in this wallet
        e = expect_rpc_error(alice.z_listreceivedbyaddress, alice_taddr2)
        assert_equal(e.error['code'], -8)
        assert_in_message(e, "bare receiver")
        print("  Case 5f PASSED")

        # ================================================================
        # Case 6: asOfHeight
        # ================================================================
        print("Case 6: asOfHeight...")
        r_before_height = alice.z_listreceivedbyaddress(
            alice_ua, MIN_CONFIRMATIONS, self_send_height - 1)
        assert_true(
            all(e['txid'] != txid_self for e in r_before_height),
            "asOfHeight below the receive's height must not surface it")

        r_at_height = alice.z_listreceivedbyaddress(
            alice_ua, MIN_CONFIRMATIONS, self_send_height)
        # The external receive and its change notes were all mined at this
        # height, each with exactly one height-relative confirmation.
        at_height = [e for e in r_at_height if e['txid'] == txid_self]
        assert_true(len(at_height) >= 2, "the self-send's outputs must be listed at their height")
        for e in at_height:
            assert_equal(e['confirmations'], 1)
        print("  PASSED")

        # ================================================================
        # Case 7: pagination
        # ================================================================
        print("Case 7: pagination...")
        full_listing = alice.z_listreceivedbyaddress(alice_ua)
        if len(full_listing) >= 2:
            page = alice.z_listreceivedbyaddress(
                alice_ua, MIN_CONFIRMATIONS, NO_AS_OF_HEIGHT_BOUND,
                PAGE_SIZE, PAGE_SIZE)
            assert_equal(page, [full_listing[1]])

        # Concatenating single-entry pages over the full range must reproduce
        # the full listing exactly (offset/limit are stable and ordering is
        # deterministic).
        reassembled = []
        for offset in range(len(full_listing)):
            page = alice.z_listreceivedbyaddress(
                alice_ua, MIN_CONFIRMATIONS, NO_AS_OF_HEIGHT_BOUND,
                offset, PAGE_SIZE)
            reassembled.extend(page)
        assert_equal(reassembled, full_listing)
        print("  PASSED")

        print("\nAll z_listreceivedbyaddress tests passed!")


if __name__ == '__main__':
    WalletZListReceivedByAddressTest().main()
