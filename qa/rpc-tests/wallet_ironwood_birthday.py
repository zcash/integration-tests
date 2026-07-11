#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) non-genesis wallet birthday, against the Z3 stack
# (zebrad + zaino + zallet).
#
# A wallet whose account birthday is ABOVE some pre-existing Ironwood notes must
# still reconstruct the Ironwood note-commitment tree frontier as of that
# birthday, so that a note it receives AFTER the birthday gets a correct witness
# and becomes spendable. This exercises the Ironwood treestate at a non-genesis
# starting point, which a genesis-birthday test never reaches.
#
# Setup: the default (mining) account shields coinbase into an Ironwood note,
# advancing the global Ironwood tree. A second account is then recovered with a
# birthday height ABOVE that note (z_recoveraccounts with birthday_height), and
# is paid an Ironwood note. The recovered account must both SEE and SPEND it.
#
# Was disabled pending zcash/zallet#563 ("z_importkey into a running wallet:
# imported key not scanned until restart"): z_recoveraccounts wrote the new
# account + UFVK, but nothing told the running sync engine's batch decryptor to
# reload its key set, so the recovered account's scan range was queued but
# never actually decrypted against — the wallet's tip never reported "caught
# up" for it, and the test could not proceed to the spend. zcash/zallet#563
# fixed the reload for z_importkey / z_getnewaccount / z_recoveraccount alike
# (merged 2026-07-10); re-enabled here now that CI's zallet build (tracking
# zallet's `main`) includes the fix. (The remaining half of that bug class,
# zcash/zallet#578, only affects a birthday that falls in an ALREADY-scanned
# range, which does not apply here: this test's birthday sits at the tip.)
#
# It may then still surface a real Ironwood bug: if the wallet's node backend
# reports an empty final Ironwood tree at a non-genesis birthday, the received
# note's witness is computed against the wrong frontier and never becomes
# spendable. Treat a later failure here as an Ironwood treestate bug report.
#

from decimal import Decimal

from test_framework.util import (
    Pool,
    PrivacyPolicy,
    account_balance_zat,
    assert_equal,
    assert_true,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
    wait_for_wallet_sync,
)
from test_framework.util_ironwood import (
    IronwoodTestFramework,
    shield_coinbase_into_ironwood,
)


class WalletIronwoodBirthdayTest(IronwoodTestFramework):

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct_a = w.z_listaccounts()[0]
        acct_a_uuid = acct_a['account_uuid']
        seedfp = acct_a['seedfp']
        assert_true(seedfp is not None,
                    "the mining account should have a seed fingerprint")
        ua_a = w.z_getaddressforaccount(acct_a_uuid, ['orchard'])['address']

        # ---- Pre-existing Ironwood note advances the global tree --------
        print("Account A: shield coinbase into an Ironwood note...")
        shield_coinbase_into_ironwood(node, w, taddr, ua_a, acct_a_uuid)
        # Bury it so the recovered account's birthday sits comfortably above it.
        node.generate(5)
        wait_for_wallet_sync(node, w)
        recovery_height = node.getblockcount()
        print("  Ironwood tree advanced; recovering account B at birthday {}"
              .format(recovery_height))

        # ---- Recover account B with a non-genesis birthday --------------
        recovered = w.z_recoveraccounts([{
            'name': 'birthday-account',
            'seedfp': seedfp,
            'zip32_account_index': 1,
            'birthday_height': recovery_height,
        }])
        acct_b_uuid = recovered['accounts'][0]['account_uuid']
        wait_for_wallet_sync(node, w, timeout=300)
        ua_b = w.z_getaddressforaccount(acct_b_uuid, ['orchard'])['address']
        print("  Recovered account B: {}".format(acct_b_uuid))

        # ---- Pay B an Ironwood note (received after its birthday) -------
        print("Account A -> B: send an Ironwood note to the recovered account...")
        amount = Decimal('5')
        amount_zat = int(amount * 100000000)
        opid = w.z_sendmany(
            ua_a, [{'address': ua_b, 'amount': amount}], 1, None,
            PrivacyPolicy.NO_PRIVACY)
        txid = wait_and_assert_operationid_status(w, opid)
        assert_true(txid is not None, "A -> B send should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, txid)

        # B must SEE the note (balance counts pending notes).
        assert_equal(account_balance_zat(w, acct_b_uuid, Pool.IRONWOOD),
                     amount_zat,
                     "recovered account B should receive the Ironwood note")
        print("  B received {} zat of Ironwood.".format(amount_zat))

        # ---- B must be able to SPEND the received note ------------------
        # This is the crux: it requires a correct Ironwood tree frontier at B's
        # non-genesis birthday. If the backend reports an empty tree there, the
        # note never becomes spendable and this times out.
        print("Account B: the received Ironwood note must become spendable...")
        assert_equal(
            wait_for_account_spendable(w, acct_b_uuid, Pool.IRONWOOD,
                                       min_zat=amount_zat),
            amount_zat)

        opid = w.z_sendmany(ua_b, [{'address': ua_b, 'amount': Decimal('1')}],
                            1, None)
        spend_txid = wait_and_assert_operationid_status(w, opid)
        assert_true(spend_txid is not None,
                    "B should be able to spend its Ironwood note")
        node.generate(1)
        wait_for_tx_scanned(w, spend_txid)
        assert_true('orchard' not in w.z_getbalanceforaccount(acct_b_uuid)['pools'],
                    "B's change must be Ironwood, not Orchard")
        print("  B spent its Ironwood note. PASSED")

        print("\nAll Ironwood non-genesis-birthday tests passed!")


if __name__ == '__main__':
    WalletIronwoodBirthdayTest().main()
