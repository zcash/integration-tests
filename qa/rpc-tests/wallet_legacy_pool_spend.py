#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Spending the legacy `zcashd` pool of funds through `z_sendmany`'s `ANY_TADDR`
# source, against the Z3 stack (zebrad + zaino + zallet).
#
# `zcashd` treated every transparent address in a wallet as a single pool of
# funds, and `ANY_TADDR` spent non-coinbase UTXOs from any of them. Zallet's
# accounts are separate pools, so it holds the legacy pool in ONE account: the
# one derived from the migrated `zcashd` wallet's mnemonic at ZIP 32 account
# index `ZCASHD_LEGACY_ACCOUNT_INDEX`. Two consequences shape this test:
#
#   1. The wallet must be TOLD which seed is the legacy one, because a Zallet
#      wallet may hold accounts derived from several seeds while `zcashd`'s
#      legacy semantics were defined for a single wallet. That is the
#      `features.legacy_pool_seed_fingerprint` config option, whose value
#      `migrate-zcashd-wallet` prints on import. Unset, this wallet has no legacy
#      pool at all; set to a seed with no legacy account, there is nothing to
#      spend. Phases 1 and 2 pin those two rejections, so that a misconfigured
#      wallet fails with an actionable error instead of spending something else.
#
#   2. WITHIN that account, selection is unconstrained: the proposer spends
#      whichever of the account's transparent receivers cover the payment, and
#      links them on-chain when one address does not suffice. Linking is what
#      distinguishes `ANY_TADDR` from naming a single transparent address (which
#      draws on that address's UTXOs alone; see wallet_transparent_spend.py), so
#      it requires the `AllowLinkingAccountAddresses` privacy policy. Phases 6
#      and 7 pin the rejection and the spend, funding the pool across TWO
#      addresses with neither able to cover the payment alone.
#
# The legacy account is created here with `z_recoveraccounts` at the legacy ZIP
# 32 index, rather than by migrating a `zcashd` wallet.dat: the account Zallet
# looks for is defined by (seed fingerprint, account index), so this yields the
# same account, on a regtest chain that actually holds spendable funds. The
# migration that creates it in production is covered by zcashd_key_import.py.
#
# Coinbase is not spendable this way (consensus requires it be spent to a single
# shielded output, which is `z_shieldcoinbase`'s job), so the pool is funded by
# shielding coinbase and unshielding into two of its transparent receivers.
#

from decimal import Decimal

from test_framework.config import ZalletArgs
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    ANY_TADDR,
    COIN,
    COINBASE_MATURITY,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    ZCASHD_LEGACY_ACCOUNT_INDEX,
    Pool,
    PrivacyPolicy,
    assert_equal,
    assert_in_message,
    assert_true,
    expect_rpc_error,
    first_transparent_receiver,
    start_wallets,
    stop_wallets,
    transparent_utxos,
    unified_address_for,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_stable_transparent,
    wait_for_tx_scanned,
    wait_zallets,
    zat,
)

# Value unshielded onto EACH of the legacy pool's two transparent receivers.
POOL_UTXO_AMOUNT = Decimal('3')

# Value spent out of the legacy pool. Strictly greater than POOL_UTXO_AMOUNT, so
# no single address can cover it and the proposer must link both: that is what
# makes this an `ANY_TADDR` spend rather than a single-address one.
LEGACY_SPEND_AMOUNT = Decimal('5')

# Diversifier indices are pinned so each address is stable and distinct.
DIVERSIFIER_SHIELDED = 10
DIVERSIFIER_POOL_A = 21
DIVERSIFIER_POOL_B = 22
DIVERSIFIER_ELSEWHERE = 23


class WalletLegacyPoolSpendTest(BitcoinTestFramework):

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
        account = w.z_listaccounts()[0]
        acct = account['account_uuid']
        seedfp = account['seedfp']

        ua_shielded = unified_address_for(w, acct, DIVERSIFIER_SHIELDED)

        # ---- Phase 0: mature coinbase -----------------------------------
        print("Phase 0: mine coinbase to maturity")
        node.generate(COINBASE_MATURITY + 20)
        expected_mature = node.getblockcount() - COINBASE_MATURITY + 1
        wait_for_mature_coinbase_count(w, expected_mature)

        # ---- Phase 1: no legacy pool is configured -----------------------
        # Without `features.legacy_pool_seed_fingerprint`, this wallet holds no
        # legacy pool: there is no way to know which of its seeds (or accounts)
        # `ANY_TADDR` would mean, so the request is refused rather than guessed.
        print("Phase 1: ANY_TADDR with the legacy pool disabled must be rejected")
        e = expect_rpc_error(
            w.z_sendmany,
            ANY_TADDR,
            [{'address': ua_shielded, 'amount': LEGACY_SPEND_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_LINKING_ACCOUNT_ADDRESSES)
        assert_in_message(e, "The legacy pool of funds is disabled")
        print("  rejected as expected (no legacy pool configured)")

        # ---- Phase 2: configured, but no legacy account exists -----------
        # Naming the seed is not enough: the pool is the account at the legacy
        # ZIP 32 index of that seed, and this wallet has only its regular
        # account. Restart with the option set, and check the wallet says so.
        print("Phase 2: restart with legacy_pool_seed_fingerprint=%s" % seedfp)
        stop_wallets(self.wallets)
        wait_zallets()
        self.wallets = start_wallets(
            self.num_wallets, self.options.tmpdir,
            zallet_args=[ZalletArgs(legacy_pool_seed_fingerprint=seedfp)])
        w = self.wallets[0]
        self.sync_all()

        e = expect_rpc_error(
            w.z_sendmany,
            ANY_TADDR,
            [{'address': ua_shielded, 'amount': LEGACY_SPEND_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_LINKING_ACCOUNT_ADDRESSES)
        assert_in_message(e, "holds no legacy account for seed fingerprint")
        print("  rejected as expected (seed named, but it has no legacy account)")

        # ---- Phase 3: create the legacy account --------------------------
        # The account `zcashd`'s legacy pool maps to: the wallet's own seed, at
        # the legacy ZIP 32 account index. This is what `migrate-zcashd-wallet`
        # creates when importing a `zcashd` wallet.
        print("Phase 3: recover the legacy account at ZIP 32 index %d"
              % ZCASHD_LEGACY_ACCOUNT_INDEX)
        recovered = w.z_recoveraccounts([{
            'name': 'legacy',
            'seedfp': seedfp,
            'zip32_account_index': ZCASHD_LEGACY_ACCOUNT_INDEX,
            'birthday_height': node.getblockcount(),
        }])['accounts']
        assert_equal(len(recovered), 1)
        assert_equal(recovered[0]['seedfp'], seedfp)
        assert_equal(recovered[0]['zip32_account_index'],
                     ZCASHD_LEGACY_ACCOUNT_INDEX)
        legacy_acct = recovered[0]['account_uuid']
        assert_true(legacy_acct != acct,
                    "the legacy pool must be its own account, not the regular one")

        taddr_a = first_transparent_receiver(
            w, unified_address_for(w, legacy_acct, DIVERSIFIER_POOL_A))
        taddr_b = first_transparent_receiver(
            w, unified_address_for(w, legacy_acct, DIVERSIFIER_POOL_B))
        assert_true(taddr_a != taddr_b,
                    "the pool must span two distinct transparent receivers")

        # ---- Phase 4: shield coinbase ------------------------------------
        # Coinbase cannot be spent transparently, so route it through the
        # shielded pool to obtain spendable non-coinbase transparent funds.
        print("Phase 4: shield coinbase (coinbase is not ANY_TADDR spendable)")
        result = w.z_shieldcoinbase(miner_taddr, ua_shielded)
        shielding_value = Decimal(result['shieldingValue'])
        shield_txid = wait_and_assert_operationid_status(w, result['opid'])
        assert_true(shield_txid is not None, "shielding should succeed")
        node.generate(1)
        shield_details = wait_for_tx_scanned(w, shield_txid)

        # Scanned is not the same as spendable: the note needs a position in the
        # commitment tree before the proposal builder will select it.
        shielded_zat = int((shielding_value - Decimal(shield_details['fee'])) * COIN)
        wait_for_account_spendable(w, acct, Pool.ORCHARD, min_zat=shielded_zat)
        assert_true(
            shielded_zat > zat(2 * POOL_UTXO_AMOUNT),
            "shielded balance must cover both pool UTXOs")

        # ---- Phase 5: fund the legacy pool across two addresses ----------
        # One transaction, two transparent outputs: each of the pool's receivers
        # ends up with a non-coinbase UTXO too small to cover phase 7's payment
        # on its own.
        print("Phase 5: unshield %s ZEC onto each of %s and %s"
              % (POOL_UTXO_AMOUNT, taddr_a, taddr_b))
        opid = w.z_sendmany(
            ua_shielded,
            [{'address': taddr_a, 'amount': POOL_UTXO_AMOUNT},
             {'address': taddr_b, 'amount': POOL_UTXO_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        fund_txid = wait_and_assert_operationid_status(w, opid)
        assert_true(fund_txid is not None, "funding the legacy pool should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, fund_txid)

        # The proposal builder selects transparent inputs from z_listunspent,
        # which lags the scanned tx; wait for the new UTXOs to settle. (The
        # miner's own coinbase UTXOs share this list, hence the settle barrier
        # rather than an exact count.)
        wait_for_stable_transparent(w, min_count=1)
        pool_utxos = [u for u in transparent_utxos(w, MIN_CONFIRMATIONS)
                      if u['address'] in (taddr_a, taddr_b)]
        assert_equal(len(pool_utxos), 2)
        for utxo in pool_utxos:
            assert_equal(int(utxo['valueZat']), zat(POOL_UTXO_AMOUNT))
        assert_true(
            zat(POOL_UTXO_AMOUNT) < zat(LEGACY_SPEND_AMOUNT),
            "neither address may cover the payment alone, else nothing links")

        # ---- Phase 6: the privacy policy is enforced ---------------------
        # Covering the payment takes both of the pool's addresses, which links
        # them on-chain. `AllowRevealedSenders` covers a transparent SOURCE, not
        # the linkage of two of an account's addresses, so it is insufficient.
        print("Phase 6a: ANY_TADDR under AllowRevealedSenders must be rejected")
        e = expect_rpc_error(
            w.z_sendmany,
            ANY_TADDR,
            [{'address': ua_shielded, 'amount': LEGACY_SPEND_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_REVEALED_SENDERS)
        assert_in_message(
            e, "spend transparent funds received by multiple unified")
        print("  rejected as expected (linking account addresses not permitted)")

        # Linking those addresses AND paying a transparent recipient leaves the
        # transaction with no privacy at all, which even `AllowFullyTransparent`
        # does not permit: it is `NoPrivacy` territory.
        taddr_elsewhere = first_transparent_receiver(
            w, unified_address_for(w, acct, DIVERSIFIER_ELSEWHERE))
        print("Phase 6b: ANY_TADDR to a t-addr under AllowFullyTransparent "
              "must be rejected")
        e = expect_rpc_error(
            w.z_sendmany,
            ANY_TADDR,
            [{'address': taddr_elsewhere, 'amount': LEGACY_SPEND_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
        assert_in_message(e, "This transaction would have no privacy")
        print("  rejected as expected (linked addresses plus a transparent "
              "recipient leaves no privacy)")

        # ---- Phase 7: the ANY_TADDR spend --------------------------------
        print("Phase 7: ANY_TADDR spend under AllowLinkingAccountAddresses")
        orchard_before = wait_for_account_spendable(w, acct, Pool.ORCHARD)
        opid = w.z_sendmany(
            ANY_TADDR,
            [{'address': ua_shielded, 'amount': LEGACY_SPEND_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            PrivacyPolicy.ALLOW_LINKING_ACCOUNT_ADDRESSES)
        spend_txid = wait_and_assert_operationid_status(w, opid)
        assert_true(spend_txid is not None, "the ANY_TADDR spend should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, spend_txid)

        # Both of the pool's addresses were spent: that is `ANY_TADDR` doing what
        # naming a single transparent address cannot. Read from the node, so it
        # is the on-chain truth rather than the wallet's view of it.
        assert_equal(node.getaddressbalance(taddr_a)['balance'], 0)
        assert_equal(node.getaddressbalance(taddr_b)['balance'], 0)

        raw = node.getrawtransaction(spend_txid, 1)
        assert_equal(len(raw['vin']), 2)

        # The payment is shielded, and so is the change: a transaction with a
        # shielded output is not fully transparent, so its change is not returned
        # to the transparent pool. No transparent output means the pool's funds
        # left the transparent pool entirely.
        assert_equal(len(raw['vout']), 0)
        assert_true(
            len(raw.get('orchard', {}).get('actions', [])) > 0,
            "the payment and change should be Orchard outputs")

        # The recipient account really received the value.
        wait_for_account_spendable(
            w, acct, Pool.ORCHARD,
            min_zat=orchard_before + zat(LEGACY_SPEND_AMOUNT))
        print("  spent %s ZEC from the legacy pool, drawing on both %s and %s. "
              "PASSED" % (LEGACY_SPEND_AMOUNT, taddr_a, taddr_b))

        print("\nAll legacy-pool spend tests passed!")


if __name__ == '__main__':
    WalletLegacyPoolSpendTest().main()
