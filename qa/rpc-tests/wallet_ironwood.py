#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test the Ironwood value pool (NU6.3, ZIP 2005) against the Z3 stack
# (zebrad + zaino + zallet).
#
# Ironwood notes are Orchard-shaped: they use the Orchard receiver of a unified
# address, and there is no separate "ironwood" receiver type. Once NU6.3 is
# active, funds received to an Orchard receiver are minted as version-3 Orchard
# (Ironwood) notes and tracked in the Ironwood value pool rather than Orchard.
#
# This is the Ironwood analogue of the Orchard wallet tests. It verifies that:
#   1. Shielding coinbase into an Orchard UA once NU6.3 is active creates
#      Ironwood notes: the funds appear under the `ironwood` pool of
#      z_getbalanceforaccount (NOT `orchard`), and z_gettotalbalance counts
#      them as `private`.
#   2. z_listunspent surfaces the note with `pool == "ironwood"`.
#   3. An Ironwood note can be spent with z_sendmany: the recipient account
#      receives the value into its own Ironwood pool, and the sender's Ironwood
#      change is retained (also in the Ironwood pool).
#
# Both zebrad and zallet must activate NU6.3 at the same height, so this test
# overrides both setup_nodes and setup_wallets to pass the NU6.3 activation
# heights; see `nu_activation_all_at_1_with_ironwood`.
#
# Note on wallet-DB creation: the wallet database must be CREATED with NU6.3
# already configured (see prepare_wallets_for_mining in test_framework/util.py).
# The librustzcash migrations bake network-upgrade activation heights into SQL
# views at creation time; the Ironwood shard-scan-ranges view embeds the NU6.3
# activation height, and if NU6.3 is only configured afterwards the view bakes a
# NULL height, leaving every Ironwood note permanently un-spendable.
#

from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZebraArgs, ZalletArgs
from test_framework.util import (
    COIN,
    COINBASE_MATURITY,
    Pool,
    assert_equal,
    assert_true,
    nu_activation_all_at_1_with_ironwood,
    start_nodes,
    start_wallets,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_mature_coinbase_count,
    wait_for_tx_scanned,
)


class WalletIronwoodTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'
        # Activate NU6.3 (Ironwood) at height 1. This drives both the node
        # (via setup_nodes) and the wallet (via setup_wallets).
        self.activation_heights = nu_activation_all_at_1_with_ironwood()

    def setup_nodes(self):
        args = [
            ZebraArgs(
                miner_address=addr,
                activation_heights=self.activation_heights,
            ) for addr in self.miner_addresses
        ]
        return start_nodes(self.num_nodes, self.options.tmpdir, args)

    def setup_wallets(self):
        # zallet must activate NU6.3 at the same height as zebra; otherwise
        # zebra's coinbase commits to the NU6.2 branch id while zallet expects
        # the NU6.3 branch id and rejects the first block it syncs.
        zallet_args = [
            ZalletArgs(activation_heights=self.activation_heights)
            for _ in range(self.num_wallets)
        ]
        return start_wallets(
            self.num_wallets, self.options.tmpdir, zallet_args=zallet_args)

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        # Wait for the wallet to sync so account-mutating RPCs are accepted.
        self.sync_all()

        # Account 0 already exists; give it an Orchard UA. Post-NU6.3 funds
        # received here land in the Ironwood pool.
        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']

        # Mine coinbase to maturity so z_shieldcoinbase has something to sweep.
        # (The wallet summary that z_getbalanceforaccount reads is only available
        # once the wallet has scanned past the account's birthday, so all
        # balance assertions come after this.)
        print("Mining initial blocks to mature coinbase...")
        node.generate(COINBASE_MATURITY + 20)
        expected_mature = node.getblockcount() - COINBASE_MATURITY + 1
        wait_for_mature_coinbase_count(w, expected_mature)

        # No shielded funds yet: only the mined transparent coinbase.
        assert_equal(list(w.z_getbalanceforaccount(acct)['pools'].keys()),
                     ['transparent'])

        # ---- Phase 1: shield coinbase -> Ironwood pool -----------------
        print("Phase 1: shield coinbase into an Orchard UA (mints Ironwood notes)")
        result = w.z_shieldcoinbase(taddr, ua)
        shielding_value = Decimal(result['shieldingValue'])
        assert_true(shielding_value > 0, "Expected a positive shielding value")
        txid = wait_and_assert_operationid_status(w, result['opid'])
        assert_true(txid is not None, "Shielding tx should have succeeded")

        node.generate(1)
        tx_details = wait_for_tx_scanned(w, txid)
        fee = Decimal(tx_details['fee'])
        ironwood_zec = shielding_value - fee

        # The shielded value is counted as private (z_gettotalbalance sums the
        # Ironwood pool into `private`). This holds as soon as the note is
        # scanned, before it is spendable.
        total = w.z_gettotalbalance(1, True)
        assert_equal(Decimal(total['private']), ironwood_zec,
                     "Ironwood funds should be counted as private")

        # Wait for the Ironwood note to become spendable (the confirming block
        # is already scanned, so this only rides out the brief lag before the
        # spendable-balance view catches up).
        expected_zat = int(ironwood_zec * COIN)
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.IRONWOOD, min_zat=expected_zat),
            expected_zat)

        # The funds are Ironwood, not Orchard, now that NU6.3 is active.
        pools = w.z_getbalanceforaccount(acct)['pools']
        assert_true('orchard' not in pools,
                    "Funds must be Ironwood, not Orchard, once NU6.3 is active; "
                    "got {}".format(pools))

        # z_listunspent surfaces the note under the ironwood pool.
        ironwood_notes = [u for u in w.z_listunspent(1)
                          if u['pool'] == Pool.IRONWOOD]
        assert_true(
            len(ironwood_notes) >= 1,
            "z_listunspent should report at least one ironwood note")
        assert_equal(
            sum(Decimal(n['valueZat']) for n in ironwood_notes),
            ironwood_zec * COIN)
        print("  Ironwood balance: {} ZEC (fee {}). PASSED".format(
            ironwood_zec, fee))

        # ---- Phase 2: spend an Ironwood note ---------------------------
        # Spend the Ironwood note within the account (a self-send to `ua`). This
        # consumes the Ironwood note and produces new Ironwood outputs (the
        # self-payment plus change), all owned by `acct` and all spendable
        # again. We deliberately do not send to a separately-created account:
        # viewing keys for an account created after the wallet's block scanner
        # started are not yet picked up by the scanner, so its received notes
        # would not be assigned a commitment-tree position and would never
        # become spendable — a general (non-Ironwood) wallet limitation.
        print("Phase 2: spend an Ironwood note (self-send within the account)")
        amount = Decimal('1')
        recipients = [{'address': ua, 'amount': amount}]
        # fee must be null (zallet computes the ZIP-317 fee internally).
        opid = w.z_sendmany(ua, recipients, 1, None)
        spend_txid = wait_and_assert_operationid_status(w, opid)
        assert_true(spend_txid is not None, "Ironwood spend should have succeeded")

        node.generate(1)
        spend_details = wait_for_tx_scanned(w, spend_txid)
        spend_fee = Decimal(spend_details['fee'])

        # The spent Ironwood note is consumed and its value (minus the fee)
        # returns to the account as new, spendable Ironwood notes. The value
        # stays in the Ironwood pool, never Orchard.
        expected_zat = int((ironwood_zec - spend_fee) * COIN)
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.IRONWOOD, min_zat=expected_zat),
            expected_zat)
        bal_after = w.z_getbalanceforaccount(acct)['pools']
        assert_true('orchard' not in bal_after, "Change must be Ironwood, not Orchard")
        print("  Spent an Ironwood note (fee {}); value retained as Ironwood. "
              "PASSED".format(spend_fee))

        print("\nAll Ironwood pool tests passed!")


if __name__ == '__main__':
    WalletIronwoodTest().main()
