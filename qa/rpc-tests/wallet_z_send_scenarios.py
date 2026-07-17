#!/usr/bin/env python3
# Copyright (c) 2025-2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Real-world usage scenarios for z_sendfromaccount / z_proposetransaction /
# z_finalizetransaction against the Z3 stack (zebrad + zaino + zallet).
#
# Where the per-RPC tests (wallet_z_sendfromaccount.py,
# wallet_z_proposetransaction.py, wallet_z_sendmany.py) cover the API surface,
# this file exercises the workflows the fund-source feature exists for, the way
# exchanges, OTC desks, payroll operators, and individuals would drive it:
#
#   - [taddr] array     -> an exchange sweeping specific user-deposit addresses
#                          into cold storage.
#   - "orchard"         -> batch customer withdrawals, a payroll run with memos,
#                          and a merchant payment (de-shield to a t-addr).
#   - "any_transparent" -> consolidating all transparent reserves to cold
#                          storage.
#   - "sapling"         -> a desk spending down legacy Sapling funds.
#   - propose/finalize  -> an OTC desk reviewing a settlement (the unsigned PCZT
#                          and the privacy policy it requires) before approving.
#
# Four of the scenarios exist to pin the guarantees the feature is FOR, rather
# than the happy path:
#
#   - `fund_source` ISOLATES. Naming a pool that cannot cover the payment must
#     report insufficient funds, never quietly reach into another pool. Without
#     this, "spend my Sapling" could silently spend Orchard.
#   - An address list RESTRICTS. Naming one deposit address must not spend a
#     sibling address's UTXOs, even when the account holds enough overall.
#   - `z_proposetransaction` has NO side effects. A proposal that is never
#     finalized must not move or lock funds.
#   - `z_finalizetransaction` HOLDS THE CALLER TO THE POLICY the proposal
#     reported. Acknowledging a stricter policy than the transaction needs must
#     be rejected, so a caller cannot under-acknowledge what it reveals.
#
# All balances are checked exactly per account via z_getbalances. A transparent
# fund source can only pay transparent recipients, and coinbase is spendable
# only via z_shieldcoinbase, so transparent funds are created by de-shielding.
#

from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs
from test_framework.util import (
    COINBASE_MATURITY,
    MIN_CONFIRMATIONS,
    FundSource,
    Pool,
    PrivacyPolicy,
    Receiver,
    RpcProxy,
    account_balance_zat,
    assert_equal,
    assert_in_message,
    assert_true,
    expect_rpc_error,
    first_transparent_receiver,
    nu_activation_all_at_1,
    start_nodes,
    transparent_change_address,
    transparent_input_addresses,
    unified_address_for,
    wait_and_assert_operationid_status,
    wait_for_account_balance,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_stable_mature_coinbase,
    wait_for_stable_transparent,
    wait_for_tx_scanned,
    zat,
)

# A memo is a fixed 512-byte field, so the hex encoding is 1024 characters.
MEMO_HEX_LEN = 1024

# The error zallet reports when the named fund source cannot cover the payment.
# It does NOT fall back to another pool; that is the point of the parameter.
INSUFFICIENT_FUNDS = "Insufficient balance"


def assert_txid(result: dict) -> str:
    """Assert a SendResult / finalize result carries a non-empty txid."""
    assert_true(isinstance(result, dict),
                "Expected dict result, got {}: {!r}".format(type(result), result))
    txid = result.get('txid')
    assert_true(isinstance(txid, str) and len(txid) > 0,
                "Expected a non-empty txid, got: {!r}".format(result))
    return txid


def memo_hex(text: str) -> str:
    """Encode `text` as a zallet memo: its hex, zero-padded to the 512-byte field."""
    return text.encode("utf-8").hex().ljust(MEMO_HEX_LEN, '0')


class WalletZSendScenariosTest(BitcoinTestFramework):

    def __init__(self) -> None:
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def setup_nodes(self):
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights=nu_activation_all_at_1(),
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _taddr(wallet: RpcProxy, account_uuid: str) -> str:
        """A fresh transparent receiver owned by `account_uuid`."""
        return first_transparent_receiver(
            wallet,
            unified_address_for(
                wallet, account_uuid,
                receivers=[Receiver.ORCHARD, Receiver.P2PKH]))

    @staticmethod
    def _shielded(wallet: RpcProxy, account_uuid: str,
                  receiver: Receiver = Receiver.ORCHARD) -> str:
        """A shielded address of `account_uuid` in the given pool."""
        return unified_address_for(wallet, account_uuid, receivers=[receiver])

    def _shield_coinbase(self, node, w0: RpcProxy, dest_ua: str, taddr: str,
                         count: int) -> None:
        """Shield `count` mature coinbase UTXOs into `dest_ua`."""
        # "At least count, stable" rather than an exact count, so it stays
        # correct after earlier scenarios have spent some coinbase.
        wait_for_stable_mature_coinbase(w0, min_count=count)
        shield = w0.z_shieldcoinbase(taddr, dest_ua, None, count)
        assert_equal(shield['shieldingUTXOs'], count)
        shield_txid = wait_and_assert_operationid_status(w0, shield['opid'])
        node.generate(1)
        wait_for_tx_scanned(w0, shield_txid)

    def _send(self, node, w0: RpcProxy, account_uuid: str, fund_source,
              recipients: list[dict], policy: PrivacyPolicy,
              minconf: int = MIN_CONFIRMATIONS) -> str:
        """Run a one-shot z_sendfromaccount, confirm it, and return its txid."""
        result = w0.z_sendfromaccount(
            account_uuid, fund_source, recipients, minconf, policy)
        txid = assert_txid(result)
        node.generate(1)
        wait_for_tx_scanned(w0, txid)
        return txid

    @staticmethod
    def _tx_fee(w0: RpcProxy, txid: str):
        """The fee `txid` paid, as reported by z_viewtransaction (a positive
        ZEC amount). The confirming block is already scanned by `_send`."""
        return w0.z_viewtransaction(txid)['fee']

    def _fund_taddr(self, node, w0: RpcProxy, account_uuid: str, taddr: str,
                    amount: str) -> None:
        """De-shield `amount` from the account's Orchard pool onto `taddr`."""
        wait_for_account_spendable(w0, account_uuid, Pool.ORCHARD, zat(amount))
        self._send(node, w0, account_uuid, FundSource.ORCHARD,
                   [{"address": taddr, "amount": amount}],
                   PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)

    # ------------------------------------------------------------------
    # Scenarios: the happy paths the feature exists to serve
    # ------------------------------------------------------------------

    def scenario_exchange_batch_withdrawal(self, node, w0: RpcProxy, hot: str,
                                           users: list[str]) -> None:
        # An exchange pays a batch of customer withdrawals from its hot wallet's
        # Orchard pool in a single private transaction.
        print("Scenario: exchange batch withdrawal (orchard -> N customers)...")
        amounts = ["0.5", "0.3", "0.2"]
        wait_for_account_spendable(w0, hot, Pool.ORCHARD, zat("1.1"))
        pre = [account_balance_zat(w0, u, Pool.ORCHARD) for u in users]
        recipients = [{"address": self._shielded(w0, u), "amount": a}
                      for u, a in zip(users, amounts)]
        self._send(node, w0, hot, FundSource.ORCHARD, recipients,
                   PrivacyPolicy.FULL_PRIVACY)
        for u, a, p in zip(users, amounts, pre):
            wait_for_account_balance(w0, u, Pool.ORCHARD, p + zat(a))
        print("  PASSED (3 customers paid 0.5/0.3/0.2 ZEC privately)")

    def scenario_otc_review_then_settle(self, node, w0: RpcProxy, desk: str,
                                        counterparty: str) -> None:
        # An OTC desk builds a settlement, reviews the unsigned PCZT and the
        # privacy policy it requires, then approves (finalizes) it.
        print("Scenario: OTC desk review-then-settle (propose -> finalize)...")
        cp_addr = self._shielded(w0, counterparty)
        wait_for_account_spendable(w0, desk, Pool.ORCHARD, zat("2.1"))
        proposal = w0.z_proposetransaction(
            desk, FundSource.ORCHARD, [{"address": cp_addr, "amount": "2.0"}],
            MIN_CONFIRMATIONS)
        pczt = proposal['pczt']
        required = proposal['privacy_policy']
        # Compliance review: a fully-shielded settlement should require nothing
        # weaker than FullPrivacy, and there is an inspectable PCZT to approve.
        assert_true(len(pczt) > 0, "expected a PCZT to review")
        assert_equal(required, PrivacyPolicy.FULL_PRIVACY)
        pre = account_balance_zat(w0, counterparty, Pool.ORCHARD)
        txid = assert_txid(w0.z_finalizetransaction(desk, pczt, required))
        node.generate(1)
        wait_for_tx_scanned(w0, txid)
        wait_for_account_balance(w0, counterparty, Pool.ORCHARD, pre + zat("2.0"))
        print("  PASSED (2.0 ZEC settlement reviewed and finalized)")

    def scenario_payroll_with_memos(self, node, w0: RpcProxy, treasury: str,
                                    employees: list[str]) -> None:
        # A company runs payroll: each employee is paid from the treasury's
        # Orchard pool with a memo identifying the pay period.
        print("Scenario: payroll run with memos (orchard -> employees)...")
        memo = memo_hex("Salary 2026-06")
        wait_for_account_spendable(w0, treasury, Pool.ORCHARD, zat("0.9"))
        pre = [account_balance_zat(w0, e, Pool.ORCHARD) for e in employees]
        recipients = [{"address": self._shielded(w0, e), "amount": "0.4",
                       "memo": memo} for e in employees]
        txid = self._send(node, w0, treasury, FundSource.ORCHARD, recipients,
                          PrivacyPolicy.FULL_PRIVACY)
        for e, p in zip(employees, pre):
            wait_for_account_balance(w0, e, Pool.ORCHARD, p + zat("0.4"))
        # Each employee's note carries the pay-period memo.
        tx = w0.z_viewtransaction(txid)
        with_memo = [o for o in tx.get('outputs', [])
                     if o.get('pool') in (Pool.SAPLING, Pool.ORCHARD)
                     and o.get('memo') == memo]
        assert_true(len(with_memo) >= len(employees),
                    "Expected each employee output to carry the memo; got {}".format(
                        [o.get('memo') for o in tx.get('outputs', [])]))
        print("  PASSED ({} employees paid 0.4 ZEC with pay-period memo)".format(
            len(employees)))

    def scenario_merchant_payment(self, node, w0: RpcProxy, payer: str,
                                  merchant: str) -> None:
        # An individual pays a merchant that only accepts transparent funds,
        # de-shielding from the Orchard pool.
        print("Scenario: merchant payment (orchard -> transparent recipient)...")
        merchant_taddr = self._taddr(w0, merchant)
        wait_for_account_spendable(w0, payer, Pool.ORCHARD, zat("0.4"))
        pre = account_balance_zat(w0, merchant, Pool.TRANSPARENT)
        self._send(node, w0, payer, FundSource.ORCHARD,
                   [{"address": merchant_taddr, "amount": "0.3"}],
                   PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        wait_for_account_balance(w0, merchant, Pool.TRANSPARENT, pre + zat("0.3"))
        print("  PASSED (merchant received 0.3 ZEC to a t-addr)")

    def scenario_exchange_deposit_sweep(self, node, w0: RpcProxy, hot: str,
                                        cold: str) -> None:
        # An exchange credits user deposits to distinct t-addresses, then sweeps
        # a chosen set of those deposit addresses into cold storage. The fund
        # source is the explicit list of deposit addresses.
        print("Scenario: exchange deposit sweep ([deposit taddrs] -> cold)...")
        deposits = [self._taddr(w0, hot) for _ in range(3)]
        assert_equal(len(set(deposits)), 3)
        wait_for_account_spendable(w0, hot, Pool.ORCHARD, zat("3.1"))
        self._send(node, w0, hot, FundSource.ORCHARD,
                   [{"address": d, "amount": "1.0"} for d in deposits],
                   PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        wait_for_stable_transparent(w0, min_count=3, minconf=MIN_CONFIRMATIONS)
        # Sweep them to cold storage. Spending several t-addresses in one
        # transaction publicly links them, so the desk acknowledges NoPrivacy.
        cold_taddr = self._taddr(w0, cold)
        pre = account_balance_zat(w0, cold, Pool.TRANSPARENT)
        self._send(node, w0, hot, deposits,
                   [{"address": cold_taddr, "amount": "2.5"}],
                   PrivacyPolicy.NO_PRIVACY)
        wait_for_account_balance(w0, cold, Pool.TRANSPARENT, pre + zat("2.5"))
        print("  PASSED (3 deposit addresses swept 2.5 ZEC to cold)")

    def scenario_reserve_consolidation(self, node, w0: RpcProxy, hot: str,
                                       cold: str) -> None:
        # The exchange consolidates its transparent reserves into cold storage
        # with the any_transparent fund source.
        print("Scenario: transparent reserve consolidation (any_transparent -> cold)...")
        reserve = self._taddr(w0, hot)
        self._fund_taddr(node, w0, hot, reserve, "1.5")
        wait_for_stable_transparent(w0, min_count=1, minconf=MIN_CONFIRMATIONS)
        cold_taddr = self._taddr(w0, cold)
        pre = account_balance_zat(w0, cold, Pool.TRANSPARENT)
        # Consolidating every transparent UTXO links the addresses holding
        # them, so this fully-transparent sweep requires NoPrivacy.
        self._send(node, w0, hot, FundSource.ANY_TRANSPARENT,
                   [{"address": cold_taddr, "amount": "1.0"}],
                   PrivacyPolicy.NO_PRIVACY)
        wait_for_account_balance(w0, cold, Pool.TRANSPARENT, pre + zat("1.0"))
        print("  PASSED (transparent reserves consolidated 1.0 ZEC to cold)")

    def scenario_mining_pool_payout_gathers_fragmented_funds(
            self, node, w0: RpcProxy, external_miner: str,
            pool_miners: list[str]) -> None:
        # zcash/zallet#644: a mining pool whose payouts are transparent -> transparent
        # (exchanges reject deposits with shielded inputs) fragments its own
        # transparent funds. Every t->t send returns change to a fresh internal
        # change address the operator never chose, and greedy selection leaves
        # tails on those addresses. The account then "reports the total as
        # spendable" but no z_sendmany source can gather it into one payout: no
        # single address holds enough. `any_transparent` is the fix -- it selects
        # non-coinbase UTXOs from ANY of the account's transparent addresses
        # (internal change included) and can never reach a shielded note, so the
        # gathered payout stays exchange-compatible. This reproduces the miner's
        # exact pipeline on a FRESH account (the bug is about a wallet created
        # from scratch), then pays the fragmented round in a single transaction.
        print("Scenario: mining-pool payout gathers fragmented transparent funds "
              "(any_transparent, zcash/zallet#644)...")

        # ---- Phase 0: a fresh pool wallet, funded as the miner funds it -------
        # coinbase -> shield -> de-shield onto the pool's own payout t-addrs.
        pool = w0.z_getnewaccount("mining-pool")['account_uuid']
        pool_ua = self._shielded(w0, pool)
        pool_a = self._taddr(w0, pool)
        pool_b = self._taddr(w0, pool)
        assert_true(len({pool_a, pool_b}) == 2, "pool payout addresses must differ")
        self._shield_coinbase(node, w0, pool_ua, self.miner_addresses[0], 4)
        self._fund_taddr(node, w0, pool, pool_a, "2.0")
        self._fund_taddr(node, w0, pool, pool_b, "2.0")
        wait_for_account_balance(w0, pool, Pool.TRANSPARENT, zat("4.0"))

        # ---- Phase 1: the wallet fragments its own funds ----------------------
        # A t->t send from pool_a paying an external miner 0.5. The greedy
        # selector spends pool_a's whole 2.0 UTXO; the ~1.5 change lands on a
        # wallet-chosen internal change address, not on pool_a or pool_b.
        ext_taddr = self._taddr(w0, external_miner)
        txid1 = self._send(node, w0, pool, [pool_a],
                           [{"address": ext_taddr, "amount": "0.5"}],
                           PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
        fee1 = self._tx_fee(w0, txid1)

        # Recover the change address and prove the wallet moved the funds to an
        # address the operator never chose.
        change_addr = transparent_change_address(node, txid1, ext_taddr)
        assert_true(change_addr not in (pool_a, pool_b, ext_taddr),
                    "change went to {}, one of the addresses the operator chose; "
                    "expected a wallet-generated internal change address".format(
                        change_addr))

        # The pool still owns that change: 4.0 - 0.5 paid out - fee remains,
        # split across pool_b (2.0) and the new change address (~1.5 - fee).
        expected_transparent = zat("4.0") - zat("0.5") - zat(fee1)
        wait_for_account_balance(w0, pool, Pool.TRANSPARENT, expected_transparent)

        # ---- Phase 2: the #644 failure mode, as a genuine precondition --------
        # A payout round of three miners totalling 3.0. This exceeds every single
        # address's holding (pool_b: 2.0; change: ~1.5) yet the account's reported
        # transparent balance covers it -- "balance says spendable, but no single
        # address can pay" is precisely the bug.
        amounts = ["1.2", "1.0", "0.8"]
        recipients = [{"address": self._taddr(w0, m), "amount": a}
                      for m, a in zip(pool_miners, amounts)]

        # Gate on z_listunspent (the signal the proposal builder's input
        # selection tracks) before attempting any transparent send.
        wait_for_stable_transparent(w0, min_count=2, minconf=MIN_CONFIRMATIONS)
        # The account covers the round, but each of its two transparent addresses
        # (read on-chain from the node) holds strictly less than it: pool_b 2.0,
        # the change fragment ~1.5. This is the reported situation exactly.
        assert_true(account_balance_zat(w0, pool, Pool.TRANSPARENT) >= zat("3.0"),
                    "the account's transparent balance should cover the 3.0 round")
        per_address = {addr: node.getaddressbalance(addr)['balance']
                       for addr in (pool_b, change_addr)}
        assert_true(max(per_address.values()) < zat("3.0"),
                    "no single address should cover the 3.0 round; holdings were "
                    "{}".format(per_address))

        # Naming a single address must fail: pool_b alone cannot cover 3.0.
        e = expect_rpc_error(
            w0.z_sendfromaccount, pool, [pool_b], recipients,
            MIN_CONFIRMATIONS, PrivacyPolicy.NO_PRIVACY)
        assert_in_message(e, INSUFFICIENT_FUNDS)

        # Under-acknowledgement must fail: covering the round forces >= 2 input
        # addresses, and linking them with transparent recipients needs NoPrivacy,
        # so AllowFullyTransparent is refused and no funds move.
        pre_miner = [account_balance_zat(w0, m, Pool.TRANSPARENT) for m in pool_miners]
        e = expect_rpc_error(
            w0.z_sendfromaccount, pool, FundSource.ANY_TRANSPARENT, recipients,
            MIN_CONFIRMATIONS, PrivacyPolicy.ALLOW_FULLY_TRANSPARENT)
        assert_true(len(e.error['message']) > 0,
                    "Expected a privacy-policy rejection for a linking t->t payout")
        node.generate(1)
        for m, p in zip(pool_miners, pre_miner):
            assert_equal(account_balance_zat(w0, m, Pool.TRANSPARENT), p)

        # ---- Phase 3: the fix -- one transaction pays the whole round ---------
        # The account still holds Orchard notes at this moment; the payout must
        # NOT touch them (exchanges reject deposits whose tx has shielded inputs).
        assert_true(account_balance_zat(w0, pool, Pool.ORCHARD) > 0,
                    "the pool should still hold Orchard notes to prove they are "
                    "not spent")
        pre_miner = [account_balance_zat(w0, m, Pool.TRANSPARENT) for m in pool_miners]
        txid3 = self._send(node, w0, pool, FundSource.ANY_TRANSPARENT, recipients,
                           PrivacyPolicy.NO_PRIVACY)
        for m, a, p in zip(pool_miners, amounts, pre_miner):
            wait_for_account_balance(w0, m, Pool.TRANSPARENT, p + zat(a))

        # The payout gathered the fragments: >= 2 transparent inputs, from more
        # than one distinct address, one of which is the Phase-1 change address.
        raw = node.getrawtransaction(txid3, 1)
        spent_inputs = [vin for vin in raw['vin'] if 'txid' in vin]
        assert_true(len(spent_inputs) >= 2,
                    "expected the payout to gather >= 2 transparent inputs, got "
                    "{}".format(len(spent_inputs)))
        input_addrs = {a for addrs in transparent_input_addresses(node, txid3)
                       for a in addrs}
        assert_true(len(input_addrs) >= 2,
                    "expected inputs from > 1 distinct address, got {}".format(
                        input_addrs))
        assert_true(change_addr in input_addrs,
                    "expected the Phase-1 change fragment {} to be spent; inputs "
                    "came from {}".format(change_addr, input_addrs))

        # And it spent NO shielded notes, so an exchange will accept the deposit.
        assert_equal(len(raw['vjoinsplit']), 0)
        assert_equal(len(raw.get('vShieldedSpend', [])), 0)
        assert_equal(len(raw.get('orchard', {}).get('actions', [])), 0)
        print("  PASSED (3 miners paid 1.2/1.0/0.8 in one tx; gathered the "
              "internal change fragment, spent no shielded notes)")

    def scenario_sapling_legacy_payout(self, node, w0: RpcProxy, hot: str,
                                       sapling_ua: str, recipient: str) -> None:
        # A desk holding legacy Sapling funds pays a counterparty from the
        # Sapling pool. The Orchard recipient/change make this cross-pool, so it
        # requires AllowRevealedAmounts.
        print("Scenario: legacy Sapling payout (sapling -> recipient)...")
        self._shield_coinbase(node, w0, sapling_ua, self.miner_addresses[0], 2)
        wait_for_account_spendable(w0, hot, Pool.SAPLING, zat("0.6"))
        r_addr = self._shielded(w0, recipient)
        pre = account_balance_zat(w0, recipient, Pool.ORCHARD)
        self._send(node, w0, hot, FundSource.SAPLING,
                   [{"address": r_addr, "amount": "0.5"}],
                   PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        wait_for_account_balance(w0, recipient, Pool.ORCHARD, pre + zat("0.5"))
        print("  PASSED (0.5 ZEC paid from the legacy Sapling pool)")

    # ------------------------------------------------------------------
    # Scenarios: the guarantees the feature is FOR
    # ------------------------------------------------------------------

    def scenario_fund_source_isolates_the_pool(self, node, w0: RpcProxy,
                                               desk: str) -> None:
        # THE core guarantee. A treasury names the pool to spend from; if that
        # pool cannot cover the payment, the send must FAIL rather than quietly
        # reach into another pool. Otherwise "spend my Sapling" could silently
        # spend Orchard, which is precisely the accounting error the parameter
        # exists to prevent.
        print("Scenario: a fund source isolates its pool (does not fall back)...")
        empty = w0.z_getnewaccount("isolation-desk")['account_uuid']
        orchard_ua = self._shielded(w0, empty)

        # Give the account Orchard funds, and NO Sapling funds.
        self._shield_coinbase(node, w0, orchard_ua, self.miner_addresses[0], 2)
        wait_for_account_spendable(w0, empty, Pool.ORCHARD, zat("1.0"))
        orchard_before = account_balance_zat(w0, empty, Pool.ORCHARD)
        assert_equal(account_balance_zat(w0, empty, Pool.SAPLING), 0)

        # Ask for Sapling. The account is not short of money overall, only of
        # SAPLING money, so a fallback bug would succeed here.
        e = expect_rpc_error(
            w0.z_sendfromaccount,
            empty, FundSource.SAPLING,
            [{"address": self._shielded(w0, desk), "amount": "0.5"}],
            MIN_CONFIRMATIONS, PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        assert_in_message(e, INSUFFICIENT_FUNDS)

        # The Orchard funds were not touched.
        node.generate(1)
        assert_equal(account_balance_zat(w0, empty, Pool.ORCHARD), orchard_before)
        print("  PASSED (sapling source refused; the Orchard pool was not raided)")

    def scenario_address_list_restricts_to_named_addresses(
            self, node, w0: RpcProxy, hot: str, cold: str) -> None:
        # An exchange sweeping ONE user's deposit address must not spend another
        # user's deposit address, even though both belong to the same account and
        # together hold enough. The address list is a restriction, not a hint.
        print("Scenario: an address list restricts to exactly those addresses...")
        addr_a = self._taddr(w0, hot)
        addr_b = self._taddr(w0, hot)
        self._fund_taddr(node, w0, hot, addr_a, "1.0")
        self._fund_taddr(node, w0, hot, addr_b, "1.0")
        wait_for_stable_transparent(w0, min_count=2, minconf=MIN_CONFIRMATIONS)

        cold_taddr = self._taddr(w0, cold)

        # 1.5 exceeds addr_a alone, but addr_a + addr_b would cover it. Naming
        # only addr_a must therefore fail: reaching into addr_b would spend a
        # different user's deposit.
        e = expect_rpc_error(
            w0.z_sendfromaccount,
            hot, [addr_a],
            [{"address": cold_taddr, "amount": "1.5"}],
            MIN_CONFIRMATIONS, PrivacyPolicy.NO_PRIVACY)
        assert_in_message(e, INSUFFICIENT_FUNDS)

        # Naming both succeeds, proving the funds really were available and the
        # refusal above was the restriction doing its job, not a shortfall.
        pre = account_balance_zat(w0, cold, Pool.TRANSPARENT)
        self._send(node, w0, hot, [addr_a, addr_b],
                   [{"address": cold_taddr, "amount": "1.5"}],
                   PrivacyPolicy.NO_PRIVACY)
        wait_for_account_balance(w0, cold, Pool.TRANSPARENT, pre + zat("1.5"))
        print("  PASSED (one address refused 1.5; both together paid it)")

    def scenario_proposal_is_side_effect_free(self, node, w0: RpcProxy, desk: str,
                                              counterparty: str) -> None:
        # A desk builds a settlement for review and then abandons it (the deal
        # falls through). Proposing must not move or lock funds: the desk's
        # balance is unchanged and the money is still spendable afterwards.
        print("Scenario: an abandoned proposal spends nothing...")
        cp_addr = self._shielded(w0, counterparty)
        wait_for_account_spendable(w0, desk, Pool.ORCHARD, zat("1.1"))
        before = account_balance_zat(w0, desk, Pool.ORCHARD)

        proposal = w0.z_proposetransaction(
            desk, FundSource.ORCHARD, [{"address": cp_addr, "amount": "1.0"}],
            MIN_CONFIRMATIONS)
        assert_true(len(proposal['pczt']) > 0, "expected a PCZT")

        # Never finalized. Mine a block to give any (incorrect) broadcast a
        # chance to confirm, then confirm nothing moved.
        node.generate(1)
        assert_equal(account_balance_zat(w0, desk, Pool.ORCHARD), before)

        # And the funds are still spendable: the proposal did not lock them.
        pre = account_balance_zat(w0, counterparty, Pool.ORCHARD)
        self._send(node, w0, desk, FundSource.ORCHARD,
                   [{"address": cp_addr, "amount": "1.0"}],
                   PrivacyPolicy.FULL_PRIVACY)
        wait_for_account_balance(w0, counterparty, Pool.ORCHARD, pre + zat("1.0"))
        print("  PASSED (proposal moved nothing; the funds remained spendable)")

    def scenario_finalize_holds_caller_to_the_reported_policy(
            self, node, w0: RpcProxy, desk: str, merchant: str) -> None:
        # `z_proposetransaction` reports the policy a transaction requires.
        # `z_finalizetransaction` must hold the caller to it: acknowledging a
        # STRICTER policy than the transaction needs is a caller who has not
        # understood what it reveals, and must be refused rather than signed.
        print("Scenario: finalize refuses an under-acknowledged privacy policy...")
        merchant_taddr = self._taddr(w0, merchant)
        wait_for_account_spendable(w0, desk, Pool.ORCHARD, zat("0.4"))

        proposal = w0.z_proposetransaction(
            desk, FundSource.ORCHARD,
            [{"address": merchant_taddr, "amount": "0.3"}], MIN_CONFIRMATIONS)
        pczt = proposal['pczt']

        # Paying a transparent recipient reveals it, so the proposal says so.
        assert_equal(proposal['privacy_policy'],
                     PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)

        # Claiming FullPrivacy for a transaction that reveals a recipient is
        # exactly the under-acknowledgement finalize exists to catch.
        e = expect_rpc_error(
            w0.z_finalizetransaction, desk, pczt, PrivacyPolicy.FULL_PRIVACY)
        assert_in_message(e, "does not permit this transaction")

        # Acknowledging what it actually reveals is accepted.
        pre = account_balance_zat(w0, merchant, Pool.TRANSPARENT)
        txid = assert_txid(w0.z_finalizetransaction(
            desk, pczt, PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS))
        node.generate(1)
        wait_for_tx_scanned(w0, txid)
        wait_for_account_balance(w0, merchant, Pool.TRANSPARENT, pre + zat("0.3"))
        print("  PASSED (FullPrivacy refused; the reported policy accepted)")

    def run_test(self) -> None:
        node = self.nodes[0]
        w0 = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        accounts = w0.z_listaccounts()
        assert_true(len(accounts) >= 1, "Wallet 0 should have at least one account")
        hot = accounts[0]['account_uuid']

        orchard_ua = self._shielded(w0, hot, Receiver.ORCHARD)
        sapling_ua = self._shielded(w0, hot, Receiver.SAPLING)

        # Counterparties: customers, an OTC counterparty, employees, a cold
        # storage account, a merchant, and a Sapling-payout recipient.
        users = [w0.z_getnewaccount("customer%d" % i)['account_uuid'] for i in range(3)]
        counterparty = w0.z_getnewaccount("otc-counterparty")['account_uuid']
        employees = [w0.z_getnewaccount("employee%d" % i)['account_uuid'] for i in range(2)]
        cold = w0.z_getnewaccount("cold-storage")['account_uuid']
        merchant = w0.z_getnewaccount("merchant")['account_uuid']
        recipient = w0.z_getnewaccount("sapling-recipient")['account_uuid']
        # A mining pool's payout counterparties (zcash/zallet#644): an external
        # miner it pays once, then a round of three miners it pays in one tx.
        external_miner = w0.z_getnewaccount("external-miner")['account_uuid']
        pool_miners = [w0.z_getnewaccount("pool-miner%d" % i)['account_uuid']
                       for i in range(3)]

        print("Mining initial blocks to mature coinbase...")
        node.generate(COINBASE_MATURITY + 20)
        expected_mature = node.getblockcount() - COINBASE_MATURITY + 1
        wait_for_mature_coinbase_count(w0, expected_mature)
        print("  Hot wallet account: {}".format(hot))

        print("\n==== Funding the hot wallet's Orchard pool ====")
        self._shield_coinbase(node, w0, orchard_ua, taddr, 6)
        wait_for_account_spendable(w0, hot, Pool.ORCHARD, zat("1.0"))
        print("  Orchard balance: {} zat".format(account_balance_zat(w0, hot, Pool.ORCHARD)))

        print("\n==== Scenarios: the workflows the feature serves ====")
        self.scenario_exchange_batch_withdrawal(node, w0, hot, users)
        self.scenario_otc_review_then_settle(node, w0, hot, counterparty)
        self.scenario_payroll_with_memos(node, w0, hot, employees)
        self.scenario_merchant_payment(node, w0, hot, merchant)
        self.scenario_exchange_deposit_sweep(node, w0, hot, cold)
        self.scenario_reserve_consolidation(node, w0, hot, cold)
        self.scenario_mining_pool_payout_gathers_fragmented_funds(
            node, w0, external_miner, pool_miners)
        self.scenario_sapling_legacy_payout(node, w0, hot, sapling_ua, recipient)

        print("\n==== Scenarios: the guarantees the feature is for ====")
        self.scenario_fund_source_isolates_the_pool(node, w0, counterparty)
        self.scenario_address_list_restricts_to_named_addresses(node, w0, hot, cold)
        self.scenario_proposal_is_side_effect_free(node, w0, hot, counterparty)
        self.scenario_finalize_holds_caller_to_the_reported_policy(
            node, w0, hot, merchant)

        print("\nAll z_send scenario tests passed!")


if __name__ == '__main__':
    WalletZSendScenariosTest().main()
