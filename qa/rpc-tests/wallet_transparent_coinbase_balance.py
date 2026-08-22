#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test the transparent regular/coinbase balance split against the Z3 stack
# (zebrad + zaino + zallet). Covers zcash/zallet#635:
#
#   z_getbalances : each account's `transparent` pool is split into
#     `regular`  : non-coinbase transparent funds (payable to a transparent
#                  recipient), and
#     `coinbase` : coinbase-originated funds (spendable only by shielding).
#                  Immature coinbase is reported under `pending`, mature
#                  coinbase under `spendable`.
#     The account's `total` object includes transparent value in its
#     spendable/pending buckets (total == sum of parts). A Balance object is
#     { spendable: {valueZat}, pending: {valueZat}, dust: {valueZat} } with
#     empty buckets omitted, and the `regular`/`coinbase` keys themselves are
#     omitted when the whole bucket is empty.
#
#   z_listunspent : transparent entries gain `"generated": true/false`
#     (coinbase origin). Shielded entries never carry a `generated` key.
#     Immature coinbase does not appear in z_listunspent at all (upstream
#     maturity clause — unchanged).
#
#   z_gettotalbalance : semantics intentionally unchanged; its `transparent`
#     figure must equal the sum of the regular and coinbase buckets reported
#     by z_getbalances.
#
# This test asserts the raw z_getbalances JSON shape directly — its purpose
# is the response contract — while using the util polling helpers to ride
# out wallet-scan lag (zcash/wallet#316).
#

import json
import time

from decimal import Decimal
from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs
from test_framework.util import (
    COINBASE_MATURITY,
    Pool,
    TotalBalanceField,
    assert_equal,
    assert_shieldcoinbase_preflight_shape,
    assert_true,
    first_transparent_receiver,
    nu_activation_all_at_1,
    start_nodes,
    transparent_utxos,
    wait_and_assert_operationid_status,
    wait_for_mature_coinbase_count,
    wait_for_total_balance,
    wait_for_tx_scanned,
    zat,
)

# The shielded pools an account section of z_getbalances may report.
SHIELDED_POOLS = (Pool.SAPLING, Pool.ORCHARD, Pool.IRONWOOD)

# Buckets a z_getbalances Balance object may carry (empty ones are omitted).
BALANCE_BUCKETS = ('spendable', 'locked', 'pending', 'dust')


def bucket_zat(balance, bucket):
    """The zatoshi value of `bucket` ('spendable' / 'locked' / 'pending' /
    'dust') in a z_getbalances Balance object. Balance objects omit empty
    buckets, and a whole pool / sub-pool key is omitted when it holds
    nothing, so both `balance` and the bucket may be absent -> 0."""
    if not balance:
        return 0
    component = balance.get(bucket)
    if not component:
        return 0
    return component['valueZat']


def balance_total_zat(balance):
    """The sum of every bucket of a z_getbalances Balance object, in
    zatoshis. 0 if the whole Balance object is omitted."""
    return sum(bucket_zat(balance, bucket) for bucket in BALANCE_BUCKETS)


def account_section(balances, account_uuid):
    """The account section of a z_getbalances response for `account_uuid`,
    or None if the response does not (yet) list the account."""
    for acct in balances.get('accounts', []):
        if acct.get('account_uuid') == account_uuid:
            return acct
    return None


def transparent_bucket_zat(balances, account_uuid, sub_pool, bucket):
    """The zatoshi value of `bucket` in the `sub_pool` ('regular' /
    'coinbase') transparent Balance object of `account_uuid`, tolerating
    every omitted-when-empty level of the response."""
    acct = account_section(balances, account_uuid)
    if acct is None:
        return 0
    tbal = acct.get(Pool.TRANSPARENT) or {}
    return bucket_zat(tbal.get(sub_pool), bucket)


def parts_zat(acct, bucket):
    """Sum `bucket` across every value pool of an account section (both
    transparent sub-pools plus each shielded pool), in zatoshis. The
    account's `total` object must equal this, bucket by bucket."""
    tbal = acct.get(Pool.TRANSPARENT) or {}
    total = (bucket_zat(tbal.get('regular'), bucket)
             + bucket_zat(tbal.get('coinbase'), bucket))
    for pool in SHIELDED_POOLS:
        total += bucket_zat(acct.get(pool), bucket)
    return total


def shielded_spendable_zat(balances, account_uuid):
    """Sum of the `spendable` buckets across the account's shielded pools."""
    acct = account_section(balances, account_uuid)
    return sum(bucket_zat(acct.get(pool), 'spendable')
               for pool in SHIELDED_POOLS)


def utxo_zat(utxo):
    """The value of a z_listunspent entry in zatoshis. Prefers the exact
    integer `valueZat` field; falls back to converting the decimal
    `amount`."""
    if 'valueZat' in utxo:
        return utxo['valueZat']
    return zat(utxo['amount'])


def assert_generated_contract(unspent):
    """Assert the z_listunspent `generated` contract on a full listing:
    every transparent entry carries a boolean `generated` key, and no
    shielded entry carries one at all."""
    for u in unspent:
        if u.get('pool') == Pool.TRANSPARENT:
            assert_true('generated' in u,
                        "Transparent entry missing 'generated': {!r}".format(u))
            assert_true(isinstance(u['generated'], bool),
                        "'generated' must be a bool: {!r}".format(u))
        else:
            assert_true('generated' not in u,
                        "Shielded entry must not carry 'generated': {!r}".format(u))


class WalletTransparentCoinbaseBalanceTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
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
                activation_heights=nu_activation_all_at_1(),
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def wait_for_balances(self, wallet, predicate, timeout=300, settle_secs=5):
        """
        Poll `z_getbalances(1)` until `predicate(response)` holds AND the
        response has been byte-identical for `settle_secs` consecutive
        seconds, then return that response. The stability window rides out
        the balance summary lagging the wallet tip by a block, and yields an
        internally consistent snapshot to run exact-equality shape
        assertions against (zcash/wallet#316).
        """
        deadline = time.time() + timeout
        last_serialized = None
        stable_secs = 0
        balances = None
        while time.time() < deadline:
            try:
                balances = wallet.z_getbalances(1)
                if predicate(balances):
                    serialized = json.dumps(
                        balances, sort_keys=True, default=str)
                    if serialized == last_serialized:
                        stable_secs += 1
                    else:
                        stable_secs = 0
                    last_serialized = serialized
                    if stable_secs >= settle_secs:
                        return balances
                else:
                    stable_secs = 0
                    last_serialized = None
            except Exception:
                pass
            time.sleep(1)
        raise AssertionError(
            "wait_for_balances: timeout after {}s; last response: {!r}".format(
                timeout, balances))

    # ------------------------------------------------------------------
    # Phase 1: only immature coinbase exists.
    # ------------------------------------------------------------------

    def run_immature_phase(self, node, w0, account_uuid):
        print("\n==== Phase 1: immature coinbase only ====")
        # `prepare_chain` mined block 1; a few more keep every coinbase
        # well under COINBASE_MATURITY confirmations.
        node.generate(4)
        self.sync_all()
        tip = node.getblockcount()
        assert_true(tip < COINBASE_MATURITY - 10,
                    "Phase 1 premise broken: tip {} too close to maturity".format(tip))

        balances = self.wait_for_balances(
            w0,
            lambda b: transparent_bucket_zat(
                b, account_uuid, 'coinbase', 'pending') > 0)
        acct = account_section(balances, account_uuid)
        assert_true(acct is not None,
                    "Account {} missing from z_getbalances: {!r}".format(
                        account_uuid, balances))

        tbal = acct.get(Pool.TRANSPARENT)
        assert_true(tbal is not None,
                    "transparent pool missing while holding immature coinbase: "
                    "{!r}".format(acct))
        coinbase = tbal.get('coinbase')
        assert_true(coinbase is not None,
                    "transparent.coinbase missing: {!r}".format(tbal))

        # Immature coinbase is pending, never spendable.
        coinbase_pending = bucket_zat(coinbase, 'pending')
        assert_true(coinbase_pending > 0,
                    "Expected pending immature coinbase, got {!r}".format(coinbase))
        assert_equal(bucket_zat(coinbase, 'spendable'), 0,
                     "Immature coinbase must not be spendable: {!r}".format(coinbase))

        # No non-coinbase funds exist, so the regular bucket is omitted.
        assert_true(tbal.get('regular') is None,
                    "transparent.regular must be absent with only coinbase "
                    "funds: {!r}".format(tbal))

        # The account total's pending bucket covers the immature coinbase.
        total = acct.get('total')
        assert_true(total is not None, "total missing: {!r}".format(acct))
        assert_true(bucket_zat(total, 'pending') >= coinbase_pending,
                    "total.pending {} must include coinbase pending {}".format(
                        bucket_zat(total, 'pending'), coinbase_pending))

        # z_listunspent shows NO coinbase UTXOs here: the upstream maturity
        # clause (unchanged by zcash/zallet#635) excludes immature coinbase
        # from z_listunspent entirely — it only ever surfaces once mature.
        # The wallet holds nothing but immature coinbase, so the listing has
        # no transparent entries at all.
        unspent = w0.z_listunspent(1)
        assert_generated_contract(unspent)
        assert_equal(
            [u for u in unspent if u.get('pool') == Pool.TRANSPARENT], [],
            "Immature coinbase must not appear in z_listunspent")

        print("  Immature coinbase pending: {} zat. PASSED".format(coinbase_pending))
        return coinbase_pending

    # ------------------------------------------------------------------
    # Phase 2: coinbase matures; spendable-by-shielding.
    # ------------------------------------------------------------------

    def run_mature_phase(self, node, w0, w0_taddr, account_uuid):
        print("\n==== Phase 2: mature coinbase ====")
        node.generate(COINBASE_MATURITY + 5)
        # A block at height H has (tip - H + 1) confirmations, so blocks
        # 1..(tip - 99) are mature: (tip - COINBASE_MATURITY + 1) in total.
        # Nothing has been spent yet.
        expected_mature = node.getblockcount() - COINBASE_MATURITY + 1
        mature = wait_for_mature_coinbase_count(w0, expected_mature)
        mature_sum = sum(utxo_zat(u) for u in mature)
        print("  Mature coinbase UTXOs: {} ({} zat)".format(
            expected_mature, mature_sum))

        balances = self.wait_for_balances(
            w0,
            lambda b: transparent_bucket_zat(
                b, account_uuid, 'coinbase', 'spendable') == mature_sum)
        acct = account_section(balances, account_uuid)
        coinbase = acct[Pool.TRANSPARENT]['coinbase']
        coinbase_spendable = bucket_zat(coinbase, 'spendable')
        assert_true(coinbase_spendable > 0,
                    "Mature coinbase must be spendable: {!r}".format(coinbase))
        assert_equal(coinbase_spendable, mature_sum,
                     "coinbase.spendable must equal the mature coinbase UTXO sum")
        # The 99 blocks above the maturity horizon are still pending.
        assert_true(bucket_zat(coinbase, 'pending') > 0,
                    "Immature tail should still be pending: {!r}".format(coinbase))
        assert_true(acct[Pool.TRANSPARENT].get('regular') is None,
                    "Still no regular transparent funds expected")

        # total == sum of parts, bucket by bucket, within one snapshot.
        total = acct.get('total')
        assert_true(total is not None, "total missing: {!r}".format(acct))
        for bucket in ('spendable', 'pending'):
            assert_equal(bucket_zat(total, bucket), parts_zat(acct, bucket),
                         "total.{} must equal the sum of parts".format(bucket))
        assert_true(bucket_zat(total, 'spendable') >= coinbase_spendable,
                    "total.spendable must include mature coinbase")

        # Every transparent z_listunspent entry is a miner-address coinbase
        # here, and each must be flagged generated == True.
        unspent = w0.z_listunspent(1)
        assert_generated_contract(unspent)
        t_entries = [u for u in unspent if u.get('pool') == Pool.TRANSPARENT]
        assert_true(len(t_entries) > 0, "Expected mature coinbase entries")
        for u in t_entries:
            assert_equal(u.get('address'), w0_taddr)
            assert_equal(u['generated'], True,
                         "Coinbase UTXO must have generated == True: {!r}".format(u))

        print("  coinbase.spendable == {} zat across {} UTXOs. PASSED".format(
            coinbase_spendable, len(t_entries)))
        return mature, mature_sum

    # ------------------------------------------------------------------
    # Phase 3: create a regular (non-coinbase) UTXO and check the split.
    # ------------------------------------------------------------------

    def run_regular_utxo_phase(self, node, w0, w0_taddr, account_uuid,
                               ua_shielded, taddr_b, mature, mature_sum):
        print("\n==== Phase 3: regular UTXO alongside coinbase ====")

        # Shield every mature coinbase UTXO into the account's shielded pools.
        result = w0.z_shieldcoinbase(w0_taddr, ua_shielded)
        assert_shieldcoinbase_preflight_shape(result)
        assert_equal(result['shieldingUTXOs'], len(mature),
                     "Sweep should select every mature coinbase UTXO")
        assert_equal(result['remainingUTXOs'], 0)
        shielding_value = Decimal(result['shieldingValue'])
        shield_txid = wait_and_assert_operationid_status(w0, result['opid'])
        assert_true(shield_txid is not None, "Shielding should have succeeded")

        node.generate(1)
        shield_fee = Decimal(wait_for_tx_scanned(w0, shield_txid)['fee'])
        # No shielded funds existed before, so `private` lands at exactly
        # shieldingValue - fee.
        expected_private = shielding_value - shield_fee
        got_private = wait_for_total_balance(
            w0, TotalBalanceField.PRIVATE, lambda v: v == expected_private)
        assert_equal(got_private, expected_private)

        # Send shielded funds to a second wallet-owned taddr, creating a
        # regular (non-coinbase) transparent UTXO. AllowRevealedRecipients
        # permits the transparent output while still forbidding transparent
        # (coinbase) inputs, so the send is guaranteed to be funded from the
        # shielded pools. fee must be null: zallet computes ZIP-317 fees
        # internally.
        send_zec = Decimal('2.5')
        send_zat = zat(send_zec)

        # `private` (via z_gettotalbalance) counts pending value, but the
        # freshly-shielded note must become *spendable* before it can fund a
        # send: the wallet lags between a note being confirmed and being
        # reported (and usable) as spendable (zcash/wallet#316). Wait on the
        # spendable bucket, with ZIP-317 fee headroom, before proposing.
        self.wait_for_balances(
            w0,
            lambda b: shielded_spendable_zat(b, account_uuid)
            >= send_zat + zat(Decimal('0.001')))

        opid = w0.z_sendmany(
            ua_shielded, [{'address': taddr_b, 'amount': send_zec}],
            1, None, 'AllowRevealedRecipients')
        send_txid = wait_and_assert_operationid_status(w0, opid)
        assert_true(send_txid is not None, "Send to taddr should have succeeded")
        node.generate(1)
        wait_for_tx_scanned(w0, send_txid)

        # The new UTXO must land in the regular bucket, and only there.
        balances = self.wait_for_balances(
            w0,
            lambda b: transparent_bucket_zat(
                b, account_uuid, 'regular', 'spendable') == send_zat)
        acct = account_section(balances, account_uuid)
        tbal = acct[Pool.TRANSPARENT]
        assert_equal(bucket_zat(tbal.get('regular'), 'spendable'), send_zat,
                     "regular.spendable must equal the amount sent to the taddr")
        assert_equal(bucket_zat(tbal.get('regular'), 'pending'), 0,
                     "Confirmed regular UTXO must not be pending")

        # The coinbase bucket now reflects only the coinbase that remained
        # after the sweep (the blocks that matured since; the two generate(1)
        # calls above matured two more). It must match the mature generated
        # UTXOs z_listunspent reports, and be strictly below the swept sum.
        coinbase_spendable = bucket_zat(tbal.get('coinbase'), 'spendable')
        mature_now = [u for u in transparent_utxos(w0, COINBASE_MATURITY)
                      if u.get('generated') is True]
        assert_equal(coinbase_spendable,
                     sum(utxo_zat(u) for u in mature_now),
                     "coinbase.spendable must equal the remaining mature "
                     "coinbase UTXO sum")
        assert_true(coinbase_spendable < mature_sum,
                    "Sweep must have reduced spendable coinbase "
                    "({} !< {})".format(coinbase_spendable, mature_sum))

        # z_listunspent: the new UTXO is present with generated == False,
        # coinbase entries stay generated == True, and shielded change notes
        # carry no generated key.
        unspent = w0.z_listunspent(1)
        assert_generated_contract(unspent)
        regular_entries = [u for u in unspent
                           if u.get('pool') == Pool.TRANSPARENT
                           and u.get('address') == taddr_b]
        assert_equal(len(regular_entries), 1,
                     "Expected exactly one UTXO on the second taddr: "
                     "{!r}".format(unspent))
        assert_equal(regular_entries[0]['generated'], False,
                     "Non-coinbase UTXO must have generated == False")
        assert_equal(utxo_zat(regular_entries[0]), send_zat)
        shielded_entries = [u for u in unspent
                            if u.get('pool') != Pool.TRANSPARENT]
        assert_true(len(shielded_entries) > 0,
                    "Expected shielded notes after the shielding sweep")

        # total.spendable == shielded spendable + regular + mature coinbase,
        # computed from the same response, to the zatoshi.
        total = acct.get('total')
        assert_true(total is not None, "total missing: {!r}".format(acct))
        shielded_spendable = sum(
            bucket_zat(acct.get(pool), 'spendable') for pool in SHIELDED_POOLS)
        assert_true(shielded_spendable > 0,
                    "Shielded funds must be spendable by now: {!r}".format(acct))
        assert_equal(
            bucket_zat(total, 'spendable'),
            shielded_spendable + send_zat + coinbase_spendable,
            "total.spendable must equal shielded + regular + mature coinbase")
        assert_equal(bucket_zat(total, 'pending'), parts_zat(acct, 'pending'),
                     "total.pending must equal the sum of parts")

        print("  regular.spendable == {} zat; coinbase.spendable == {} zat. "
              "PASSED".format(send_zat, coinbase_spendable))
        return balances

    # ------------------------------------------------------------------
    # Phase 4: z_gettotalbalance cross-check.
    # ------------------------------------------------------------------

    def run_totalbalance_crosscheck(self, w0, account_uuid, balances):
        print("\n==== Phase 4: z_gettotalbalance cross-check ====")
        # z_gettotalbalance's semantics are intentionally unchanged by
        # zcash/zallet#635: its `transparent` figure (in ZEC decimal) is the
        # whole transparent holding, which must equal the regular + coinbase
        # bucket totals of z_getbalances. The chain is idle here, so the
        # snapshot from phase 3 is still current; poll the summary until it
        # converges (its scan tip can lag, zcash/wallet#316).
        acct = account_section(balances, account_uuid)
        tbal = acct[Pool.TRANSPARENT]
        expected_zat = (balance_total_zat(tbal.get('regular'))
                        + balance_total_zat(tbal.get('coinbase')))
        got = wait_for_total_balance(
            w0, TotalBalanceField.TRANSPARENT,
            lambda v: zat(v) == expected_zat)
        assert_equal(zat(got), expected_zat,
                     "z_gettotalbalance.transparent must equal the "
                     "regular + coinbase totals from z_getbalances")
        print("  transparent == {} ZEC ({} zat). PASSED".format(got, expected_zat))

    # ------------------------------------------------------------------

    def run_test(self):
        node = self.nodes[0]
        w0 = self.wallets[0]

        # `prepare_wallets_for_mining` captures the raw zallet CLI stdout;
        # normalize for string comparison against z_listunspent's encoding.
        w0_taddr = self.miner_addresses[0].strip()

        # Wait for the wallet to sync to the node tip. Account-mutating RPCs
        # reject with "Wallet sync required" until the wallet has committed
        # at least one block, since they anchor derivations to a chain height.
        self.sync_all()

        accounts = w0.z_listaccounts()
        assert_true(len(accounts) >= 1, "Wallet 0 should have at least one account")
        account_uuid = accounts[0]['account_uuid']

        # Shielding destination owned by the same account. Sapling+Orchard
        # receivers let the change strategy pick whichever pool it prefers
        # (pure-Orchard many-UTXO shields hit a fee-estimation bug, see
        # wallet_z_shieldcoinbase.py).
        ua_shielded = w0.z_getaddressforaccount(
            account_uuid, ["sapling", "orchard"])['address']

        # A second wallet-owned transparent address (EXTERNAL scope), distinct
        # from the INTERNAL-scope miner address: the destination for the
        # regular (non-coinbase) UTXO in phase 3.
        ua_with_taddr = w0.z_getaddressforaccount(
            account_uuid, ["orchard", "p2pkh"])['address']
        taddr_b = first_transparent_receiver(w0, ua_with_taddr)
        assert_true(taddr_b != w0_taddr,
                    "Second taddr must differ from the miner address")
        print("  Account UUID:    {}".format(account_uuid))
        print("  Miner taddr:     {}".format(w0_taddr))
        print("  Second taddr:    {}".format(taddr_b))

        self.run_immature_phase(node, w0, account_uuid)
        mature, mature_sum = self.run_mature_phase(
            node, w0, w0_taddr, account_uuid)
        balances = self.run_regular_utxo_phase(
            node, w0, w0_taddr, account_uuid, ua_shielded, taddr_b,
            mature, mature_sum)
        self.run_totalbalance_crosscheck(w0, account_uuid, balances)

        print("\nAll transparent coinbase balance tests passed!")


if __name__ == '__main__':
    WalletTransparentCoinbaseBalanceTest().main()
