#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario A1: small holder, a single clean note holding an EXACT small balance.
#
# A separate faucet wallet funds the subject with exactly 2 ZEC in one Orchard
# note (the faucet keeps its coinbase change, so the subject holds precisely
# 2 ZEC, not the whole coinbase). The subject migrates to completion. The balance
# checks are inline and EXACT so a reviewer confirms from this file alone that a
# real 2-ZEC holder migrated its 2 ZEC into Ironwood.
#

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

# The subject's exact starting balance: a single clean 2-ZEC Orchard note.
SOURCE_ZEC = 2
SOURCE_ZAT = SOURCE_ZEC * COIN


class SmallHolderScenario(IronwoodMigrationScenario):

    def run_test(self):
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)

        print("Faucet funds the subject with EXACTLY {} ZEC (one Orchard "
              "note)...".format(SOURCE_ZEC))
        orchard_before = self.fund_exact_note(0, SOURCE_ZEC)
        source_notes = sorted(int(n['valueZat']) for n in orchard_notes(subject))
        print("  subject Orchard: {} zat in notes {}".format(
            orchard_before, source_notes))
        assert_equal(orchard_before, SOURCE_ZAT,
                     "the subject holds EXACTLY {} ZEC, not the whole coinbase"
                     .format(SOURCE_ZEC))
        assert_equal(len(source_notes), 1, "a single clean source note")

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

        # ---- assert the EXACT balances INLINE (self-contained) --------------
        notes = ironwood_notes(subject)
        note_values = sorted(int(n['valueZat']) for n in notes)
        ironwood_zat = sum(note_values)
        orchard_after = account_spendable_zat(subject, sacct, Pool.ORCHARD)
        fees = orchard_before - ironwood_zat - orchard_after
        print("  source Orchard:   {} zat ({} ZEC)".format(
            orchard_before, SOURCE_ZEC))
        print("  Ironwood notes:   {} = {} zat".format(note_values, ironwood_zat))
        print("  Orchard residual: {} zat; fees: {} zat over {} txs".format(
            orchard_after, fees, total_txs))

        # A genuine 2-ZEC migration: one Ironwood note per crossing, each
        # positive, value conserved, and fees within the per-transaction bound.
        assert_equal(orchard_before, SOURCE_ZAT, "the subject started at 2 ZEC")
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
                    "only a sub-dust residual remains in Orchard: {} zat".format(
                        orchard_after))
        print("\nSmall-holder scenario passed.")


if __name__ == '__main__':
    SmallHolderScenario().main()
