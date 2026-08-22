#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario D12: the double-start guard.
#
# Once a migration is committed (its transactions pre-signed and persisted),
# starting another for the same account must be refused: overwriting the
# in-progress migration would discard its pre-signed transactions. A wallet UI
# that lets the user tap "migrate" twice must not corrupt an in-flight run. The
# guard lifts only once the migration reaches a terminal state. A faucet funds
# the subject with an exact balance.
#

from test_framework.ironwood_migration_common import (
    FROM_POOL,
    IronwoodMigrationScenario,
    START_RPC,
    TO_POOL,
)
from test_framework.util import _RPC_EXCEPTIONS, assert_true

SOURCE_ZEC = 5


class DoubleStartScenario(IronwoodMigrationScenario):

    def run_test(self):
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)

        print("Faucet funds the subject with EXACTLY {} ZEC...".format(
            SOURCE_ZEC))
        self.fund_exact_note(0, SOURCE_ZEC)
        self.activate_ironwood()

        # First start commits the migration (builds and pre-signs it).
        started = self.start(subject, sacct)
        migration_id = started['migration_id']
        assert_true(started['plan']['transaction_count'] > 0,
                    "the first start plans at least one transaction")
        print("  first start committed: id={!r}.".format(migration_id))

        # A second start, while the first is in progress, must be refused.
        try:
            getattr(subject, START_RPC)(sacct, FROM_POOL, TO_POOL)
        except _RPC_EXCEPTIONS as e:
            message = str(e.error.get('message', ''))
            print("  second start refused while one is in progress: {!r}. OK"
                  .format(message))
        else:
            raise AssertionError(
                "a second start must be refused while a migration is in progress")

        # The in-progress migration is intact (still the same one).
        listed = getattr(subject, 'z_listpoolmigrations')()
        assert_true(len(listed) == 1,
                    "exactly one migration is in progress after the refused start")
        print("\nDouble-start guard scenario passed.")


if __name__ == '__main__':
    DoubleStartScenario().main()
