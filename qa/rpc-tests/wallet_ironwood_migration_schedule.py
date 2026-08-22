#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario E1: the transfer broadcast schedule is spread for privacy.
#
# To avoid linking the crossings, the migration does not broadcast every transfer
# at once: each is scheduled at a different height across a window. A faucet funds
# the subject with an EXACT balance; the scenario starts a migration and asserts
# inline that the transfer transactions carry distinct scheduled heights spanning
# a range of blocks, so a driver broadcasts them spread out rather than at once.
#

from test_framework.ironwood_migration_common import IronwoodMigrationScenario
from test_framework.util import assert_true

SOURCE_ZEC = 60


class ScheduleScenario(IronwoodMigrationScenario):

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

        started = self.start(subject, sacct)
        migration_id = started['migration_id']

        st = self.status(subject, migration_id)
        transfers = [t for t in st['transactions'] if t['kind'] == 'transfer']
        heights = sorted(t['scheduled_height'] for t in transfers)
        print("  {} transfers; scheduled heights: {}".format(
            len(transfers), heights))

        assert_true(len(transfers) >= 2,
                    "a multi-crossing migration has several transfers")
        # The transfers are spread across a range of heights, not all at once.
        assert_true(len(set(heights)) > 1,
                    "transfers are scheduled across multiple heights: {}".format(
                        heights))
        span = heights[-1] - heights[0]
        assert_true(span > 0,
                    "the transfer schedule spans a range of blocks (span {})"
                    .format(span))
        print("  transfers spread across {} distinct heights, spanning {} "
              "blocks. OK".format(len(set(heights)), span))
        print("\nSchedule scenario passed.")


if __name__ == '__main__':
    ScheduleScenario().main()
