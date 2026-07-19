#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario C4: progress reporting monotonicity.
#
# A wallet renders a progress indicator from the migration status. Under normal
# operation progress must only move FORWARD: the count of completed (mined)
# transactions never decreases, the phase never regresses from in_progress back
# to a pre-start phase, and completion happens exactly when every transaction is
# mined. A faucet funds the subject with many exact notes (a multi-layer
# migration), and this scenario asserts monotonicity at every step.
#

from decimal import Decimal

from test_framework.ironwood_migration_common import IronwoodMigrationScenario
from test_framework.util import (
    COIN,
    Pool,
    account_spendable_zat,
    assert_equal,
    assert_true,
    ironwood_notes,
)

# Many source notes -> a multi-layer, multi-transaction migration to exercise
# progress monotonicity over a long run.
SOURCE_NOTES = [Decimal('12')] * 10
SOURCE_ZAT = int(sum(SOURCE_NOTES) * COIN)

# The ordered lifecycle phases, so a regression can be detected by index.
PHASE_ORDER = ['not_started', 'in_progress', 'completed']


class MonotonicProgressScenario(IronwoodMigrationScenario):

    def run_test(self):
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)

        print("Faucet funds the subject with {} exact notes ({} ZEC)...".format(
            len(SOURCE_NOTES), int(sum(SOURCE_NOTES))))
        orchard_before = self.fund_exact_orchard(0, SOURCE_NOTES)
        assert_equal(orchard_before, SOURCE_ZAT, "exact source balance")
        self.activate_ironwood()

        expected_crossings = self.preview(subject, sacct)['funding_note_count']
        started = self.start(subject, sacct)
        migration_id = started['migration_id']
        total = started['plan']['transaction_count']
        print("  started: {} transactions, {} crossings.".format(
            total, expected_crossings))

        state = {'completed': 0, 'phase_idx': 0}

        def on_step(step, adv):
            progress = adv['progress']
            completed = progress['completed_transactions']
            assert_true(completed >= state['completed'],
                        "completed count regressed: {} -> {}".format(
                            state['completed'], completed))
            assert_true(progress['total_transactions'] == total,
                        "the total transaction count must not change mid-run")
            phase = adv['phase']
            if phase in PHASE_ORDER:
                idx = PHASE_ORDER.index(phase)
                assert_true(idx >= state['phase_idx'],
                            "phase regressed: {} -> {}".format(
                                PHASE_ORDER[state['phase_idx']], phase))
                state['phase_idx'] = idx
            state['completed'] = completed
            if phase == 'completed':
                assert_true(completed == total,
                            "completion must mean every transaction is mined")

        completed = self.drive_to_completion(subject, sacct, migration_id,
                                             on_step=on_step)
        assert_true(completed,
                    "the migration completed within {} advance steps".format(
                        self.MAX_ADVANCE_STEPS))

        # ---- assert the exact balances INLINE (self-contained) --------------
        notes = ironwood_notes(subject)
        note_values = sorted(int(n['valueZat']) for n in notes)
        ironwood_zat = sum(note_values)
        orchard_after = account_spendable_zat(subject, sacct, Pool.ORCHARD)
        fees = orchard_before - ironwood_zat - orchard_after
        print("  source Orchard:   {} zat".format(orchard_before))
        print("  Ironwood notes:   {} = {} zat".format(note_values, ironwood_zat))
        print("  Orchard residual: {} zat; fees: {} zat over {} txs".format(
            orchard_after, fees, total))
        assert_equal(len(note_values), expected_crossings,
                     "one Ironwood note per crossing")
        assert_true(all(v > 0 for v in note_values),
                    "every Ironwood note holds a positive balance")
        assert_true(ironwood_zat + orchard_after <= orchard_before,
                    "no value was created")
        assert_true(0 <= fees <= total * 200000,
                    "fees {} zat within the {} zat bound".format(
                        fees, total * 200000))
        assert_true(orchard_after < 1000000,
                    "only a sub-dust residual remains in Orchard: {} zat".format(
                        orchard_after))
        print("  progress advanced monotonically to {}/{}. OK".format(
            total, total))
        print("\nMonotonic-progress scenario passed.")


if __name__ == '__main__':
    MonotonicProgressScenario().main()
