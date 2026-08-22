#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario A5: dust-heavy wallet (many small Orchard notes).
#
# A wallet whose Orchard balance is fragmented into many sub-funding-value dust
# notes stresses the migration's preparation, which must consolidate the dust
# rather than strand it. A faucet funds the subject with one small base note plus
# twelve dust notes (EXACT amounts), then the migration is driven to completion
# and the exact balances are asserted inline: the Ironwood note count equals the
# plan's crossing count, every note is positive, value is conserved, and only a
# sub-dust residual is left in Orchard (the dust was swept, not stranded).
#

from decimal import Decimal

from test_framework.ironwood_migration_common import (
    IronwoodMigrationScenario,
    orchard_notes,
)
from test_framework.util import (
    COIN,
    Pool,
    account_spendable_zat,
    assert_equal,
    assert_true,
    ironwood_notes,
)

# One base note plus twelve dust notes (0.02 ZEC each): a dust-heavy source.
SOURCE_NOTES = [Decimal('1')] + [Decimal('0.02')] * 12
SOURCE_ZAT = int(sum(SOURCE_NOTES) * COIN)


class DustHeavyScenario(IronwoodMigrationScenario):

    def run_test(self):
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)

        print("Faucet funds the subject with a base note plus dust notes...")
        orchard_before = self.fund_exact_orchard(0, SOURCE_NOTES)
        source_notes = sorted(int(n['valueZat']) for n in orchard_notes(subject))
        print("  {} Orchard source notes = {} zat".format(
            len(source_notes), source_notes))
        assert_equal(orchard_before, SOURCE_ZAT, "exact source balance")
        assert_equal(len(source_notes), len(SOURCE_NOTES),
                     "the source is a dust-heavy {}-note wallet".format(
                         len(SOURCE_NOTES)))

        self.activate_ironwood()
        preview = self.preview(subject, sacct)
        expected_crossings = preview['funding_note_count']
        print("  preview: {} layer(s), {} funding note(s).".format(
            preview['preparation']['layer_count'], expected_crossings))

        started = self.start(subject, sacct)
        migration_id = started['migration_id']
        total_txs = started['plan']['transaction_count']
        print("  started: transaction_count={}".format(total_txs))

        completed = self.drive_to_completion(subject, sacct, migration_id)
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
            orchard_after, fees, total_txs))

        assert_equal(len(note_values), expected_crossings,
                     "one Ironwood note per crossing")
        assert_true(all(v > 0 for v in note_values),
                    "every Ironwood note holds a positive balance")
        assert_true(ironwood_zat + orchard_after <= orchard_before,
                    "no value was created")
        assert_true(0 <= fees <= total_txs * 200000,
                    "fees {} zat within the {} zat bound".format(
                        fees, total_txs * 200000))
        assert_true(orchard_after < 1000000,
                    "the dust was swept; only a sub-dust residual remains: {} "
                    "zat".format(orchard_after))
        print("\nDust-heavy scenario passed.")


if __name__ == '__main__':
    DustHeavyScenario().main()
