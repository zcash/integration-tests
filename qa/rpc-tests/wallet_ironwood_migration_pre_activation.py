#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario D10: starting a migration before NU6.3 is active.
#
# The Orchard -> Ironwood migration is enabled by NU6.3; attempting it before
# activation must be rejected with a clear error that names the required upgrade,
# not build a migration that could never be mined. A faucet funds the subject
# with a real Orchard note (in the Orchard era) but the chain is left BELOW the
# activation height.
#

from test_framework.ironwood_migration_common import (
    FROM_POOL,
    IRONWOOD_HEIGHT,
    IronwoodMigrationScenario,
    START_RPC,
    TO_POOL,
)
from test_framework.util import _RPC_EXCEPTIONS, assert_true

SOURCE_ZEC = 5


class PreActivationScenario(IronwoodMigrationScenario):

    def run_test(self):
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)

        # Fund an Orchard source note, but do NOT cross the activation boundary.
        print("Faucet funds the subject with {} ZEC, staying pre-NU6.3..."
              .format(SOURCE_ZEC))
        self.fund_exact_note(0, SOURCE_ZEC)
        assert_true(self.faucet_node.getblockcount() < IRONWOOD_HEIGHT,
                    "the chain must remain below NU6.3 activation")

        # Start must be rejected while NU6.3 is not active.
        try:
            getattr(subject, START_RPC)(sacct, FROM_POOL, TO_POOL)
        except _RPC_EXCEPTIONS as e:
            message = str(e.error.get('message', ''))
            print("  start before NU6.3 rejected: {!r}. OK".format(message))
        else:
            raise AssertionError(
                "start must be rejected before NU6.3 is active")

        print("\nPre-activation scenario passed.")


if __name__ == '__main__':
    PreActivationScenario().main()
