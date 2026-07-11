#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Transparent-to-transparent (t-to-t) spending through `z_sendmany`, against the
# Z3 stack (zebrad + zaino + zallet).
#
# Zallet spends transparent funds only when the `fromaddress` is a bare
# transparent address: the selector is then restricted to that one address's
# UTXOs (`TransparentSpendPolicy::from_one_address`), so a shielded send can
# never silently reach into transparent funds, and the named address is not
# linked to the account's other transparent receivers.
#
# Two consensus/policy facts shape this test:
#
#   1. Coinbase UTXOs are NOT spendable here. Consensus requires coinbase to be
#      spent to a single shielded output with no change, which is what
#      `z_shieldcoinbase` is for. A t-to-t spend therefore needs a NON-coinbase
#      transparent UTXO, and on regtest the only way to get one is to shield
#      coinbase and then send shielded -> transparent. That is phases 1 and 2.
#
#   2. A transaction that both spends transparent funds and has transparent
#      recipients or change reveals everything, so it requires the
#      `AllowFullyTransparent` privacy policy. `AllowRevealedSenders` (which
#      only covers a transparent SOURCE paying a shielded recipient) is
#      insufficient; phase 3 pins that rejection.
#
# Change on a fully-transparent send stays transparent (it goes to an
# internal-scope BIP 44 change address), rather than being swept into a
# shielded pool.
# Phase 4 asserts that, since it is what makes the t-to-t send actually
# transparent end to end.
#

from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    Pool,
    PrivacyPolicy,
    assert_equal,
    assert_in_message,
    assert_true,
    expect_rpc_error,
    first_transparent_receiver,
    transparent_change_address,
    unified_address_for,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_stable_transparent,
    wait_for_tx_scanned,
)

# Value sent shielded -> transparent to create the non-coinbase UTXO that the
# t-to-t spend then draws on.
UNSHIELD_AMOUNT = Decimal('10')

# Value moved transparent -> transparent. Strictly less than UNSHIELD_AMOUNT so
# the spend must produce transparent change.
TT_AMOUNT = Decimal('4')


# Diversifier indices are pinned so each address is stable and distinct.
DIVERSIFIER_SHIELDED = 10
DIVERSIFIER_SRC = 11
DIVERSIFIER_DST = 12


class WalletTransparentSpendTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        miner_taddr = self.miner_addresses[0]

        self.sync_all()
        acct = w.z_listaccounts()[0]['account_uuid']

        ua_shielded = unified_address_for(w, acct, DIVERSIFIER_SHIELDED)
        taddr_src = first_transparent_receiver(
            w, unified_address_for(w, acct, DIVERSIFIER_SRC))
        taddr_dst = first_transparent_receiver(
            w, unified_address_for(w, acct, DIVERSIFIER_DST))
        assert_true(
            taddr_src != taddr_dst,
            "source and destination must be distinct transparent receivers")

        # ---- Phase 0: mature coinbase -----------------------------------
        print("Phase 0: mine coinbase to maturity")
        node.generate(COINBASE_MATURITY + 20)
        expected_mature = node.getblockcount() - COINBASE_MATURITY + 1
        wait_for_mature_coinbase_count(w, expected_mature)

        # ---- Phase 1: shield coinbase -----------------------------------
        # Coinbase cannot be spent transparently, so route it through the
        # shielded pool to obtain spendable non-coinbase transparent funds.
        print("Phase 1: shield coinbase (coinbase is not t-to-t spendable)")
        result = w.z_shieldcoinbase(miner_taddr, ua_shielded)
        shielding_value = Decimal(result['shieldingValue'])
        shield_txid = wait_and_assert_operationid_status(w, result['opid'])
        assert_true(shield_txid is not None, "shielding should succeed")
        node.generate(1)
        shield_details = wait_for_tx_scanned(w, shield_txid)

        # Scanned is not the same as spendable: the note needs a position in the
        # commitment tree before the proposal builder will select it.
        shielded_zat = int((shielding_value - Decimal(shield_details['fee'])) * COIN)
        wait_for_account_spendable(
            w, acct, Pool.ORCHARD, min_zat=shielded_zat)
        assert_true(
            shielded_zat > int(UNSHIELD_AMOUNT * COIN),
            "shielded balance must cover the unshielding amount")

        # ---- Phase 2: shielded -> transparent ---------------------------
        # Creates the non-coinbase UTXO at `taddr_src` that phase 4 spends.
        print("Phase 2: unshield to %s (creates a non-coinbase UTXO)" % taddr_src)
        opid = w.z_sendmany(
            ua_shielded,
            [{'address': taddr_src, 'amount': UNSHIELD_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        unshield_txid = wait_and_assert_operationid_status(w, opid)
        assert_true(unshield_txid is not None, "unshielding should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, unshield_txid)

        # The proposal builder selects transparent inputs from z_listunspent,
        # which lags the scanned tx; wait for the new UTXO to settle.
        wait_for_stable_transparent(w, min_count=1)
        src_utxos = [u for u in w.z_listunspent(MIN_CONFIRMATIONS)
                     if u['pool'] == Pool.TRANSPARENT
                     and u['address'] == taddr_src]
        assert_equal(len(src_utxos), 1)
        assert_equal(Decimal(src_utxos[0]['valueZat']), UNSHIELD_AMOUNT * COIN)
        print("  %s holds %s ZEC (non-coinbase)" % (taddr_src, UNSHIELD_AMOUNT))

        # ---- Phase 3: the privacy policy is enforced --------------------
        # A t-to-t spend reveals the sender AND the recipient, so neither half of
        # the policy is sufficient on its own. Both rejections are pinned, because
        # they come from different checks: the recipient check runs up-front on the
        # payment request, while the fully-transparent check runs on the built
        # proposal (and so is only reachable once transparent inputs are selected).
        print("Phase 3a: t-to-t under AllowRevealedSenders must be rejected")
        e = expect_rpc_error(
            w.z_sendmany,
            taddr_src,
            [{'address': taddr_dst, 'amount': TT_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_SENDERS)
        assert_in_message(e, "would have transparent recipients")
        print("  rejected as expected (transparent recipient not permitted)")

        print("Phase 3b: t-to-t under AllowRevealedRecipients must be rejected")
        e = expect_rpc_error(
            w.z_sendmany,
            taddr_src,
            [{'address': taddr_dst, 'amount': TT_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        assert_in_message(
            e, "would both spend transparent funds and have transparent")
        print("  rejected as expected (fully-transparent not permitted)")

        # ---- Phase 4: the t-to-t spend ----------------------------------
        print("Phase 4: t-to-t spend under AllowFullyTransparent")
        opid = w.z_sendmany(
            taddr_src,
            [{'address': taddr_dst, 'amount': TT_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
        tt_txid = wait_and_assert_operationid_status(w, opid)
        assert_true(tt_txid is not None, "t-to-t spend should succeed")
        node.generate(1)
        tt_details = wait_for_tx_scanned(w, tt_txid)
        fee = Decimal(tt_details['fee'])

        # The recipient holds exactly the sent value, confirmed on-chain by the
        # node rather than by wallet accounting.
        assert_equal(
            node.getaddressbalance(taddr_dst)['balance'],
            int(TT_AMOUNT * COIN))

        # The transaction is fully transparent: transparent inputs, no shielded
        # components anywhere.
        raw = node.getrawtransaction(tt_txid, 1)
        assert_true(len(raw['vin']) >= 1, "t-to-t spend must have a transparent input")
        assert_equal(len(raw['vjoinsplit']), 0)
        assert_equal(len(raw.get('vShieldedSpend', [])), 0)
        assert_equal(len(raw.get('vShieldedOutput', [])), 0)
        assert_equal(len(raw.get('orchard', {}).get('actions', [])), 0)

        # Change stayed transparent, at an internal-scope change address that is
        # neither the source nor the recipient. Sweeping it into Orchard would
        # have made this a t-to-z send wearing a t-to-t costume. (The helper
        # asserts the two-output recipient-plus-change shape.)
        change_addr = transparent_change_address(node, tt_txid, taddr_dst)
        assert_true(
            change_addr != taddr_src,
            "change must go to an internal change address, not back to the source")

        # The source UTXO is fully consumed; the change is a new transparent
        # UTXO worth the remainder.
        change_zat = int((UNSHIELD_AMOUNT - TT_AMOUNT - fee) * COIN)
        assert_equal(node.getaddressbalance(change_addr)['balance'], change_zat)
        assert_equal(node.getaddressbalance(taddr_src)['balance'], 0)
        print("  sent %s ZEC t-to-t (fee %s); change %s ZEC to %s. PASSED"
              % (TT_AMOUNT, fee, Decimal(change_zat) / COIN, change_addr))

        print("\nAll transparent-spend tests passed!")


if __name__ == '__main__':
    WalletTransparentSpendTest().main()
