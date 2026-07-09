#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Ironwood (NU6.3, ZIP 2005) persistence and transaction shape, against the Z3
# stack (zebrad + zaino + zallet).
#
# Verifies that Ironwood state survives a wallet restart and that Ironwood
# spends have the expected on-chain shape:
#   1. Persistence: after shielding into Ironwood and restarting zallet (reusing
#      the same wallet DB), the Ironwood balance is still reported, still
#      spendable, and an Ironwood note can be spent post-restart with its change
#      remaining Ironwood.
#   2. Transaction shape: an Ironwood spend is a v6 transaction (NU6.3 version
#      group), as reported by both z_viewtransaction and decoderawtransaction.
#
# Known gap (noted, not asserted): decoderawtransaction surfaces the Orchard
# bundle but not the Ironwood bundle, so the Ironwood actions do not appear in
# its decoded output; only the v6 version/version-group identify the tx.
#

from decimal import Decimal

from test_framework.util import (
    COIN,
    Pool,
    RpcProxy,
    assert_equal,
    assert_true,
    ironwood_notes,
    self_send,
    stop_wallets,
    wait_for_account_spendable,
    wait_for_wallet_sync,
    wait_zallets,
)
from test_framework.util_ironwood import (
    IronwoodTestFramework,
    ironwood_spends,
    shield_coinbase_into_ironwood,
)

# V6 (NU6.3) transaction version and version group id (zcash_protocol constants
# V6_TX_VERSION / V6_VERSION_GROUP_ID).
V6_TX_VERSION = 6
V6_VERSION_GROUP_ID = "d884b698"


class WalletIronwoodPersistenceTest(IronwoodTestFramework):

    def restart_wallet(self) -> RpcProxy:
        """Stop and restart zallet, reusing the persisted wallet DB, then block
        until it has resynced to the node tip. Returns the new RPC handle."""
        stop_wallets(self.wallets)
        wait_zallets()
        self.wallets = self.setup_wallets()
        wait_for_wallet_sync(self.nodes[0], self.wallets[0])
        return self.wallets[0]

    def run_test(self) -> None:
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']

        # ---- Fund an Ironwood note --------------------------------------
        print("Funding an Ironwood note (shield coinbase)...")
        _, ironwood_zat = shield_coinbase_into_ironwood(node, w, taddr, ua, acct)
        assert_equal(len(ironwood_notes(w)), 1)
        print("  Ironwood balance: {} zat".format(ironwood_zat))

        # ---- Test 1a: balance persists across a restart -----------------
        print("Test 1a: Ironwood balance survives a wallet restart...")
        w = self.restart_wallet()
        # The account UUID is stable across restarts (same DB).
        assert_equal(w.z_listaccounts()[0]['account_uuid'], acct)
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.IRONWOOD,
                                       min_zat=ironwood_zat),
            ironwood_zat)
        assert_equal(len(ironwood_notes(w)), 1)
        print("  PASSED")

        # ---- Test 1b: an Ironwood note is spendable post-restart --------
        print("Test 1b: spend an Ironwood note after the restart...")
        spend_txid, spend_fee = self_send(node, w, ua, Decimal('1'))
        expected_after = ironwood_zat - int(spend_fee * COIN)
        assert_equal(
            wait_for_account_spendable(w, acct, Pool.IRONWOOD,
                                       min_zat=expected_after),
            expected_after)
        assert_true('orchard' not in w.z_getbalanceforaccount(acct)['pools'],
                    "change must be Ironwood, not Orchard")
        print("  PASSED")

        # ---- Test 2: the Ironwood spend is a v6 transaction -------------
        print("Test 2: the Ironwood spend is a v6 (NU6.3) transaction...")
        view = w.z_viewtransaction(spend_txid)
        assert_equal(view['version'], V6_TX_VERSION,
                     "Ironwood spend should be a v6 transaction")
        assert_true(len(ironwood_spends(view)) >= 1,
                    "the spend should consume an Ironwood note")

        raw = w.getrawtransaction(spend_txid)
        decoded = w.decoderawtransaction(raw)
        assert_equal(decoded['version'], V6_TX_VERSION)
        assert_equal(decoded['versiongroupid'], V6_VERSION_GROUP_ID,
                     "v6 transactions use the NU6.3 version group id")
        # Note: decoderawtransaction surfaces the Orchard bundle but not the
        # Ironwood bundle, so the Ironwood actions are not present in `decoded`.
        print("  v6 tx confirmed (version group {}). PASSED"
              .format(decoded['versiongroupid']))

        print("\nAll Ironwood persistence tests passed!")


if __name__ == '__main__':
    WalletIronwoodPersistenceTest().main()
