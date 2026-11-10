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
# The subject is simply never funded.
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
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)

        # Activate NU6.3 without ever funding the subject, so it has no
        # source-pool balance to migrate.
        self.activate_ironwood()
        assert_true(len(orchard_notes(subject)) == 0,
                    "the account must have no Orchard notes for this scenario")

        # Start must reject an account with nothing to migrate.
        try:
            getattr(subject, START_RPC)(sacct, FROM_POOL, TO_POOL)
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
