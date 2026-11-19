#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Address issuance at the transparent gap limit, against the Z3 stack
# (zebrad + zallet).
#
# `z_getaddressforaccount` without a diversifier index hands out the next unused
# index. For an address with a transparent receiver, zallet does not go more than
# the gap limit past the last index that received funds, because a wallet
# recovered from its mnemonic phrase only looks that far ahead. Once every index
# in that window has been handed out:
#
#   - by default the request fails until one of the issued addresses is funded;
#   - with `rpc.issue_past_transparent_gap_limit` set, it continues at the next
#     index that has not been issued (zcash/zallet#959).
#
# An address issued past the window is an ordinary address of the wallet: this
# test checks that a deposit to one is detected and can be spent, that issuance
# never hands the same index out twice (including an index that was requested
# explicitly, and across a restart), and that the default behaviour is unchanged.
#

from decimal import Decimal

from test_framework.config import ZalletArgs
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    COIN,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    TRANSPARENT_GAP_LIMIT,
    Pool,
    PrivacyPolicy,
    Receiver,
    assert_equal,
    assert_in_message,
    assert_true,
    expect_rpc_error,
    first_transparent_receiver,
    shield_coinbase,
    start_wallets,
    stop_wallets,
    transparent_utxos,
    unified_address_for,
    wait_and_assert_operationid_status,
    wait_for_tx_scanned,
    wait_for_wallet_sync,
    wait_zallets,
)

# Receivers requested for every sequentially issued address. A transparent
# receiver is what subjects the address to the gap limit. Sapling is left out
# because about half of all indices have no valid Sapling diversifier, which
# would make the issued indices non-contiguous and the assertions below weaker.
RECEIVERS = [Receiver.P2PKH, Receiver.ORCHARD]

# How many addresses are issued past the gap window in one go: several windows'
# worth, so that crossing the window once is not mistaken for moving it.
ADDRESSES_PAST_WINDOW = 3 * TRANSPARENT_GAP_LIMIT

# How far beyond the sequentially issued indices the explicitly requested index
# lies. Small, so that sequential issuance reaches it within this test.
EXPLICIT_INDEX_OFFSET = 2

# How many addresses are issued after the explicit request: enough to pass it.
ADDRESSES_ACROSS_EXPLICIT_INDEX = 2 * EXPLICIT_INDEX_OFFSET

# How many addresses are issued after the deposit and after the restart: more
# than one, so that the order of the indices is checked as well.
ADDRESSES_PER_CHECK = 3

# The shielded address that receives the shielded coinbase and the later spend.
# Shielded-only, so it is not subject to the gap limit; pinned well away from the
# indices the test issues sequentially.
DIVERSIFIER_SHIELDED = 1000

# Value sent to an address issued past the gap window.
DEPOSIT_AMOUNT = Decimal('2')

# Value spent back out of that address. Less than DEPOSIT_AMOUNT, leaving room
# for the fee.
SPEND_AMOUNT = Decimal('1')


def next_address(wallet, account_uuid):
    """Issue the next address of `account_uuid` with `RECEIVERS`, returning its
    `(diversifier_index, address)`."""
    result = wallet.z_getaddressforaccount(account_uuid, RECEIVERS)
    return result['diversifier_index'], result['address']


def next_addresses(wallet, account_uuid, count):
    """Issue `count` addresses in sequence, returning `{diversifier_index: address}`
    after checking that the indices are strictly increasing."""
    issued = [next_address(wallet, account_uuid) for _ in range(count)]
    indices = [index for index, _ in issued]
    assert_equal(sorted(set(indices)), indices,
                 "sequential issuance must hand out increasing indices")
    return dict(issued)


class WalletTransparentGapLimitTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def restart_wallet(self, zallet_args=None):
        """Stop and restart zallet on the same wallet database, applying
        `zallet_args` if given, and block until it has caught up with the node."""
        stop_wallets(self.wallets)
        wait_zallets()
        self.wallets = start_wallets(
            self.num_wallets, self.options.tmpdir,
            zallet_args=None if zallet_args is None else [zallet_args])
        wait_for_wallet_sync(self.nodes[0], self.wallets[0])
        return self.wallets[0]

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        miner_taddr = self.miner_addresses[0]

        self.sync_all()
        acct = w.z_listaccounts()[0]['account_uuid']

        # ---- Phase 1: the default stops at the gap limit ------------------
        # Nothing has been funded, so the window is the first
        # TRANSPARENT_GAP_LIMIT indices. One of them is already taken by the
        # account's default address, so one fewer can be issued.
        print("Phase 1: by default, issuance stops at the gap limit")
        issued = next_addresses(w, acct, TRANSPARENT_GAP_LIMIT - 1)
        assert_true(
            max(issued) < TRANSPARENT_GAP_LIMIT,
            "indices issued by default must lie inside the gap window: %r"
            % sorted(issued))
        e = expect_rpc_error(w.z_getaddressforaccount, acct, RECEIVERS)
        assert_in_message(e, "reached the transparent gap limit")
        assert_in_message(e, "at index %d." % TRANSPARENT_GAP_LIMIT)
        assert_in_message(e, "rpc.issue_past_transparent_gap_limit")
        # The refusal is stable: asking again does not consume an index.
        e = expect_rpc_error(w.z_getaddressforaccount, acct, RECEIVERS)
        assert_in_message(e, "at index %d." % TRANSPARENT_GAP_LIMIT)
        print("  issued %r, then refused at index %d"
              % (sorted(issued), TRANSPARENT_GAP_LIMIT))

        # ---- Phase 2: with the setting, issuance continues ----------------
        print("Phase 2: restart with issue_past_transparent_gap_limit, issue %d more"
              % ADDRESSES_PAST_WINDOW)
        w = self.restart_wallet(
            ZalletArgs(issue_past_transparent_gap_limit=True))
        past = next_addresses(w, acct, ADDRESSES_PAST_WINDOW)
        assert_equal(
            list(range(TRANSPARENT_GAP_LIMIT,
                       TRANSPARENT_GAP_LIMIT + ADDRESSES_PAST_WINDOW)),
            sorted(past),
            "issuance must continue at the end of the window, without holes")
        issued.update(past)
        assert_equal(len(issued), len(set(issued.values())),
                     "every issued address must be distinct")

        # ---- Phase 3: an explicitly issued index is not issued again ------
        explicit_index = max(issued) + 1 + EXPLICIT_INDEX_OFFSET
        print("Phase 3: request index %d explicitly, then continue in sequence"
              % explicit_index)
        explicit_address = unified_address_for(
            w, acct, explicit_index, RECEIVERS)
        after_explicit = next_addresses(w, acct, ADDRESSES_ACROSS_EXPLICIT_INDEX)
        assert_true(
            max(after_explicit) > explicit_index,
            "sequential issuance must have passed the explicit index")
        assert_true(
            explicit_index not in after_explicit,
            "an index issued on request must be skipped: %r"
            % sorted(after_explicit))
        assert_true(
            explicit_address not in after_explicit.values(),
            "the explicitly issued address must not be handed out again")
        issued.update(after_explicit)
        issued[explicit_index] = explicit_address

        # ---- Phase 4: a deposit past the window is detected ---------------
        # The last address issued in phase 2 is several windows past the last
        # funded index (there is none), so it is well outside the window.
        deposit_index = max(past)
        deposit_taddr = first_transparent_receiver(w, issued[deposit_index])
        print("Phase 4: deposit %s ZEC to index %d (%s)"
              % (DEPOSIT_AMOUNT, deposit_index, deposit_taddr))
        ua_shielded = unified_address_for(
            w, acct, DIVERSIFIER_SHIELDED, [Receiver.ORCHARD])
        shield_coinbase(node, w, miner_taddr, ua_shielded, acct, Pool.ORCHARD)
        opid = w.z_sendmany(
            ua_shielded,
            [{'address': deposit_taddr, 'amount': DEPOSIT_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        deposit_txid = wait_and_assert_operationid_status(w, opid)
        node.generate(1)
        wait_for_tx_scanned(w, deposit_txid)
        wait_for_wallet_sync(node, w)
        deposits = [u for u in transparent_utxos(w)
                    if u.get('address') == deposit_taddr]
        assert_equal(1, len(deposits),
                     "the wallet must see the deposit past the gap window")
        assert_equal(int(DEPOSIT_AMOUNT * COIN), deposits[0]['valueZat'])
        assert_equal(deposit_txid, deposits[0]['txid'])

        # ---- Phase 5: issuance after the deposit --------------------------
        # The deposit moves the window to the indices after `deposit_index`,
        # all of which were handed out above. Issuance must carry on after the
        # highest index issued so far rather than repeat any of them.
        print("Phase 5: issuance continues after the deposit moved the window")
        highest = max(issued)
        after_deposit = next_addresses(w, acct, ADDRESSES_PER_CHECK)
        assert_equal(
            list(range(highest + 1, highest + 1 + ADDRESSES_PER_CHECK)),
            sorted(after_deposit))
        assert_true(
            set(after_deposit.values()).isdisjoint(issued.values()),
            "no address may be issued twice")
        issued.update(after_deposit)

        # ---- Phase 6: the deposit past the window is spendable ------------
        print("Phase 6: spend %s ZEC from %s" % (SPEND_AMOUNT, deposit_taddr))
        opid = w.z_sendmany(
            deposit_taddr,
            [{'address': ua_shielded, 'amount': SPEND_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_SENDERS)
        spend_txid = wait_and_assert_operationid_status(w, opid)
        node.generate(1)
        wait_for_tx_scanned(w, spend_txid)
        wait_for_wallet_sync(node, w)
        assert_equal(
            [], [u for u in transparent_utxos(w) if u['txid'] == deposit_txid],
            "the deposit must be spent")
        assert_equal(0, node.getaddressbalance(deposit_taddr)['balance'])

        # ---- Phase 7: issuance survives a restart -------------------------
        # The setting stays in the config file, so no arguments are needed.
        print("Phase 7: restart, issuance continues after the last index")
        highest = max(issued)
        w = self.restart_wallet()
        after_restart = next_addresses(w, acct, ADDRESSES_PER_CHECK)
        assert_equal(
            list(range(highest + 1, highest + 1 + ADDRESSES_PER_CHECK)),
            sorted(after_restart))
        assert_true(
            set(after_restart.values()).isdisjoint(issued.values()),
            "no address may be issued twice across a restart")

        print("\nAll transparent gap limit tests passed!")


if __name__ == '__main__':
    WalletTransparentGapLimitTest().main()
