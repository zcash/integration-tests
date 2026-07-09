#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) transaction-view surfacing, against the Z3 stack
# (zebrad + zaino + zallet).
#
# Ironwood notes are Orchard-shaped and tracked as a distinct value pool. The
# RPCs that report per-output detail (`z_viewtransaction`, `z_listtransactions`)
# and per-pool balances (`z_getbalances`) must label these notes as the
# `ironwood` pool, NOT `orchard`. This test drives a shield and a self-send and
# asserts that surfacing, covering:
#   1. z_viewtransaction: the shield's Ironwood output and the spend's Ironwood
#      spends/outputs carry pool == "ironwood" (output type "action").
#   2. z_listtransactions: the wallet's history lists both transactions with
#      ironwood outputs and correct values / change flags.
#   3. Memo round-trip: a memo sent on an Ironwood output is surfaced by
#      z_viewtransaction (`memo` hex and `memoStr`).
#   4. z_getbalances: the account's Ironwood balance appears under the
#      `ironwood` key with a spendable component.
#
# Known gap (surfaced here as a note, not a failing assertion): z_getnotescount
# does not report Ironwood notes (it counts only Sapling and Orchard). We assert
# that Ironwood notes are at least not miscounted as Orchard.
#

from decimal import Decimal

from test_framework.util import (
    Pool,
    account_balance_zat,
    account_spendable_zat,
    assert_equal,
    assert_true,
    self_send,
)
from test_framework.util_ironwood import (
    IronwoodTestFramework,
    ironwood_outputs,
    ironwood_spends,
    shield_coinbase_into_ironwood,
)


class WalletIronwoodViewsTest(IronwoodTestFramework):

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']

        # ---- Fund the account with an Ironwood note ---------------------
        print("Funding account with an Ironwood note (shield coinbase)...")
        shield_txid, ironwood_zat = shield_coinbase_into_ironwood(
            node, w, taddr, ua, acct)
        print("  Ironwood balance: {} zat".format(ironwood_zat))

        # ---- Test 1: z_viewtransaction surfaces the shield's Ironwood output
        print("Test 1: z_viewtransaction reports the shield's Ironwood output...")
        view = w.z_viewtransaction(shield_txid)
        assert_true('version' in view, "view is missing tx version")
        outs = ironwood_outputs(view)
        assert_equal(len(outs), 1,
                     "shield should create exactly one Ironwood output; got {}"
                     .format(view['outputs']))
        out = outs[0]
        assert_equal(out['valueZat'], ironwood_zat)
        assert_equal(out['account_uuid'], acct)
        assert_true('action' in out,
                    "Ironwood output must be indexed by action, got {}".format(out))
        # Shielded to the account's own external receiver: received, not change.
        assert_true(not out['walletInternal'],
                    "shield output should be external-scope, got {}".format(out))
        # No Orchard outputs once NU6.3 is active.
        assert_true(all(o['pool'] != Pool.ORCHARD for o in view['outputs']),
                    "post-NU6.3 shielding must not produce an Orchard output")
        print("  PASSED")

        # ---- Test 4 (balances): read before the spend changes them ------
        print("Test 4: z_getbalances reports the Ironwood balance...")
        assert_equal(
            account_balance_zat(w, acct, Pool.IRONWOOD), ironwood_zat)
        assert_equal(
            account_spendable_zat(w, acct, Pool.IRONWOOD), ironwood_zat)
        # The Ironwood funds must not surface under the Orchard pool.
        assert_equal(account_balance_zat(w, acct, Pool.ORCHARD), 0)
        print("  PASSED")

        # ---- Test 3: memo round-trip on an Ironwood output --------------
        print("Test 3: memo round-trips on an Ironwood self-send...")
        memo_text = "ironwood-memo-test"
        memo_hex = memo_text.encode("utf-8").hex()
        amount = Decimal('1')
        spend_txid, _ = self_send(node, w, ua, amount, memo=memo_hex)

        spend_view = w.z_viewtransaction(spend_txid)
        memo_outs = [o for o in ironwood_outputs(spend_view)
                     if o.get('memoStr') == memo_text]
        assert_equal(
            len(memo_outs), 1,
            "expected exactly one Ironwood output carrying the memo; got {}"
            .format([(o.get('memoStr'), o['valueZat'])
                     for o in ironwood_outputs(spend_view)]))
        assert_equal(memo_outs[0]['valueZat'], int(amount * 100000000))
        print("  PASSED")

        # ---- Test 1b: z_viewtransaction reports the spend's Ironwood spends
        print("Test 1b: z_viewtransaction reports the spend's Ironwood spends...")
        spends = ironwood_spends(spend_view)
        assert_true(len(spends) >= 1,
                    "spend should consume at least one Ironwood note; got {}"
                    .format(spend_view['spends']))
        assert_equal(spends[0]['txidPrev'], shield_txid,
                     "the spent Ironwood note should come from the shield tx")
        assert_equal(spends[0]['account_uuid'], acct)
        # Change returns to the account as an internal-scope Ironwood output.
        change_outs = [o for o in ironwood_outputs(spend_view)
                       if o['walletInternal']]
        assert_true(len(change_outs) >= 1,
                    "self-send should produce Ironwood change; got {}"
                    .format(spend_view['outputs']))
        print("  PASSED")

        # ---- Test 2: z_listtransactions lists both txs with ironwood pool
        print("Test 2: z_listtransactions surfaces the Ironwood outputs...")
        txs = {t['txid']: t for t in w.z_listtransactions(acct)}
        assert_true(shield_txid in txs,
                    "shield tx missing from z_listtransactions")
        assert_true(spend_txid in txs,
                    "spend tx missing from z_listtransactions")

        shield_iron = [o for o in txs[shield_txid]['outputs']
                       if o['pool'] == Pool.IRONWOOD]
        assert_equal(len(shield_iron), 1)
        assert_equal(shield_iron[0]['value'], ironwood_zat)
        assert_true(not shield_iron[0]['is_change'],
                    "shield output should not be flagged as change")

        # The spend produces an Ironwood payment (memo, not change) and Ironwood
        # change; both are surfaced under the ironwood pool.
        spend_iron = [o for o in txs[spend_txid]['outputs']
                      if o['pool'] == Pool.IRONWOOD]
        assert_true(len(spend_iron) >= 1,
                    "spend tx should list Ironwood outputs; got {}"
                    .format(txs[spend_txid]['outputs']))
        assert_true(any(o.get('memo') == memo_text for o in spend_iron),
                    "spend's Ironwood payment should carry the memo; got {}"
                    .format([o.get('memo') for o in spend_iron]))
        print("  PASSED")

        # ---- z_getnotescount gap (documented, not a hard failure) -------
        # z_getnotescount currently counts only Sapling and Orchard notes; it
        # does not surface Ironwood. Assert the Ironwood notes are at least not
        # miscounted as Orchard. (Tracked as a zallet surfacing gap.)
        counts = w.z_getnotescount()
        assert_equal(counts['orchard'], 0,
                     "Ironwood notes must not be counted as Orchard by "
                     "z_getnotescount; got {}".format(counts))
        print("Note: z_getnotescount does not report Ironwood (orchard={}, "
              "sapling={}).".format(counts['orchard'], counts['sapling']))

        print("\nAll Ironwood view-surfacing tests passed!")


if __name__ == '__main__':
    WalletIronwoodViewsTest().main()
