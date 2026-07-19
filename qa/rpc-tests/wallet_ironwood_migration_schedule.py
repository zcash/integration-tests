#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario E1: the transfer broadcast schedule is spread for privacy.
#
# To avoid linking the crossings, the migration does not broadcast every transfer
# at once: each is scheduled at a different height across a window. This scenario
# starts a migration and asserts inline that the transfer transactions carry
# distinct scheduled heights spanning a range of blocks, so a driver broadcasts
# them spread out rather than in a single burst.
#

from test_framework.ironwood_migration_common import IronwoodMigrationScenario
from test_framework.util import assert_true

SOURCE_NOTE_ZEC = 60


class ScheduleScenario(IronwoodMigrationScenario):

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        if self.skip_if_rpcs_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']

        print("Funding an Orchard balance...")
        self.fund_orchard_note(node, w, taddr, acct, SOURCE_NOTE_ZEC)
        self.activate_ironwood(node)

        started = self.start(w, acct)
        migration_id = started['migration_id']

        st = self.status(w, migration_id)
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
