#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario D10: starting a migration before NU6.3 is active.
#
# The Orchard -> Ironwood migration is enabled by NU6.3; attempting it before
# activation must be rejected with a clear error that names the required
# upgrade, not build a migration that could never be mined. The account is
# funded with a real Orchard note (in the Orchard era) but the chain is left
# BELOW the activation height.
#

from test_framework.ironwood_migration_common import (
    FROM_POOL,
    IRONWOOD_HEIGHT,
    IronwoodMigrationScenario,
    START_RPC,
    TO_POOL,
)
from test_framework.util import _RPC_EXCEPTIONS, assert_true

SOURCE_NOTE_ZEC = 5


class PreActivationScenario(IronwoodMigrationScenario):

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        if self.skip_if_rpcs_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']

        # Fund an Orchard source note, but do NOT cross the activation boundary.
        print("Funding an Orchard source ({} ZEC), staying pre-NU6.3..."
              .format(SOURCE_NOTE_ZEC))
        self.fund_orchard_note(node, w, taddr, acct, SOURCE_NOTE_ZEC)
        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "the chain must remain below NU6.3 activation")

        # Start must be rejected while NU6.3 is not active.
        try:
            getattr(w, START_RPC)(acct, FROM_POOL, TO_POOL)
        except _RPC_EXCEPTIONS as e:
            message = str(e.error.get('message', ''))
            print("  start before NU6.3 rejected: {!r}. OK".format(message))
        else:
            raise AssertionError(
                "start must be rejected before NU6.3 is active")

        print("\nPre-activation scenario passed.")


if __name__ == '__main__':
    PreActivationScenario().main()
