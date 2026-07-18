#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario D8: nothing to migrate.
#
# An account with no spendable source-pool (Orchard) balance cannot be migrated.
# Starting a migration for it must fail cleanly with a clear error, not build an
# empty migration, panic, or hang. This is the degenerate input every wallet UI
# must handle (the user taps "migrate" on an empty or transparent-only account).
#

from test_framework.ironwood_migration_common import (
    FROM_POOL,
    IronwoodMigrationScenario,
    START_RPC,
    TO_POOL,
    orchard_notes,
)
from test_framework.util import _RPC_EXCEPTIONS, assert_true


class NothingToMigrateScenario(IronwoodMigrationScenario):

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]

        self.sync_all()
        if self.skip_if_rpcs_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']

        # Activate NU6.3 without ever funding an Orchard note, so the account has
        # no source-pool balance to migrate.
        self.activate_ironwood(node)
        assert_true(len(orchard_notes(w)) == 0,
                    "the account must have no Orchard notes for this scenario")

        # Start must reject an account with nothing to migrate.
        try:
            getattr(w, START_RPC)(acct, FROM_POOL, TO_POOL)
        except _RPC_EXCEPTIONS as e:
            message = str(e.error.get('message', ''))
            print("  start on an empty account rejected: {!r}. OK".format(
                message))
        else:
            raise AssertionError(
                "start should fail for an account with no source-pool balance")

        print("\nNothing-to-migrate scenario passed.")


if __name__ == '__main__':
    NothingToMigrateScenario().main()
