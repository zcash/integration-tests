#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) negative and policy scenarios, against the Z3
# stack (zebrad + zaino + zallet).
#
# The other Ironwood tests are happy-path; this one covers the rejection and
# edge cases:
#   1. Spending more Ironwood than the account holds is rejected.
#   2. Crossing Ironwood -> Sapling under FullPrivacy is rejected (crossing the
#      turnstile reveals amounts), while the same send under
#      AllowRevealedAmounts is accepted.
#   3. An empty memo and a maximum-length (512-byte) memo both round-trip on an
#      Ironwood output.
#   4. A single z_sendmany fans out to several recipients, each paid from
#      Ironwood.
#

from decimal import Decimal

from test_framework.util import (
    COIN,
    Pool,
    PrivacyPolicy,
    account_spendable_zat,
    assert_equal,
    assert_true,
    expect_rpc_error,
    self_send,
    wait_account_settled,
    wait_and_assert_operationid_status,
    wait_for_tx_scanned,
)
from test_framework.util_ironwood import (
    IronwoodTestFramework,
    ironwood_outputs,
    shield_coinbase_into_ironwood,
)

# 512 bytes is the maximum memo size; as a hex string that is 1024 characters.
MAX_MEMO_TEXT = "ironwood"
MAX_MEMO_HEX = MAX_MEMO_TEXT.encode("utf-8").hex().ljust(1024, "0")


class WalletIronwoodNegativesTest(IronwoodTestFramework):

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']

        print("Funding an Ironwood note...")
        _, ironwood_zat = shield_coinbase_into_ironwood(node, w, taddr, ua, acct)

        # ---- Test 1: insufficient funds ---------------------------------
        print("Test 1: spending more Ironwood than held is rejected...")
        too_much = Decimal(ironwood_zat) / COIN + Decimal('1000')
        e = expect_rpc_error(
            w.z_sendmany, ua, [{'address': ua, 'amount': too_much}], 1, None)
        msg = e.error['message'].lower()
        assert_true('insufficient' in msg or 'funds' in msg,
                    "expected an insufficient-funds error; got {!r}"
                    .format(e.error['message']))
        # The failed proposal must not have moved anything.
        assert_equal(account_spendable_zat(w, acct, Pool.IRONWOOD), ironwood_zat)
        print("  PASSED ({})".format(e.error['message'][:70]))

        # ---- Test 2: paying a transparent recipient under FullPrivacy ----
        # is rejected (a transparent output reveals the recipient and amount).
        print("Test 2: Ironwood -> transparent under FullPrivacy is rejected...")
        to_t = [{'address': taddr, 'amount': Decimal('1')}]
        e = expect_rpc_error(
            w.z_sendmany, ua, to_t, 1, None, PrivacyPolicy.FULL_PRIVACY)
        assert_true(len(e.error['message']) > 0,
                    "expected a non-empty privacy-policy rejection")
        # The rejected send must not have moved anything.
        assert_equal(account_spendable_zat(w, acct, Pool.IRONWOOD), ironwood_zat)
        print("  PASSED ({})".format(e.error['message'][:70]))

        # ---- Test 2b: the same payment under NoPrivacy succeeds ----------
        print("Test 2b: the same payment under NoPrivacy succeeds...")
        opid = w.z_sendmany(ua, to_t, 1, None, PrivacyPolicy.NO_PRIVACY)
        txid = wait_and_assert_operationid_status(w, opid)
        assert_true(txid is not None,
                    "shielded -> transparent under NoPrivacy should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, txid)
        # The Ironwood note was spent; its change stays Ironwood.
        assert_true('orchard' not in w.z_getbalanceforaccount(acct)['pools'],
                    "change must be Ironwood, not Orchard")
        wait_account_settled(w, acct)
        print("  PASSED")

        # ---- Test 3: empty and max-length memos round-trip --------------
        print("Test 3: empty and maximum-length memos on Ironwood outputs...")
        # Empty memo: the Ironwood payment carries no text memo.
        empty_txid, _ = self_send(node, w, ua, Decimal('1'))
        empty_view = w.z_viewtransaction(empty_txid)
        payments = [o for o in ironwood_outputs(empty_view)
                    if not o['walletInternal']]
        assert_true(all(o.get('memoStr') is None for o in payments),
                    "empty-memo payment should have no memoStr; got {}"
                    .format([o.get('memoStr') for o in payments]))

        # Maximum-length memo (512 bytes): round-trips as text.
        wait_account_settled(w, acct)
        memo_txid, _ = self_send(node, w, ua, Decimal('1'), memo=MAX_MEMO_HEX)
        memo_view = w.z_viewtransaction(memo_txid)
        carried = [o for o in ironwood_outputs(memo_view)
                   if o.get('memoStr') == MAX_MEMO_TEXT]
        assert_equal(len(carried), 1,
                     "expected exactly one Ironwood output with the max memo; "
                     "got {}".format([o.get('memoStr')
                                      for o in ironwood_outputs(memo_view)]))
        print("  PASSED")

        # ---- Test 4: multi-recipient fan-out from Ironwood --------------
        print("Test 4: a single z_sendmany fans out to several recipients...")
        wait_account_settled(w, acct)
        ua2 = w.z_getaddressforaccount(acct, ['orchard'])['address']
        one = Decimal('1')
        fan = [{'address': ua, 'amount': one}, {'address': ua2, 'amount': one}]
        opid = w.z_sendmany(ua, fan, 1, None)
        fan_txid = wait_and_assert_operationid_status(w, opid)
        assert_true(fan_txid is not None, "fan-out send should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, fan_txid)

        fan_view = w.z_viewtransaction(fan_txid)
        payments = [o for o in ironwood_outputs(fan_view)
                    if not o['walletInternal']]
        assert_true(len(payments) >= 2,
                    "expected at least two Ironwood payment outputs; got {}"
                    .format(fan_view['outputs']))
        assert_equal(sum(1 for o in payments if o['valueZat'] == int(one * COIN)),
                     2, "expected two 1-ZEC Ironwood payments")
        print("  PASSED")

        print("\nAll Ironwood negative/policy tests passed!")


if __name__ == '__main__':
    WalletIronwoodNegativesTest().main()
