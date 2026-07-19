#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario F1: multiple parties migrating concurrently on one shared chain.
#
# On the real network, many wallets migrate Orchard -> Ironwood at the same time.
# This scenario runs THREE independent subject wallets (a small holder, a retail
# holder, and a multi-note whale whose preparation fans across several layers),
# each funded with an EXACT balance by one shared faucet, and drives all three
# migrations CONCURRENTLY: one advance step per subject per round, then a single
# mining batch that confirms every subject's just-broadcast transaction together
# (as one block on the shared chain would). It asserts that each migration
# completes independently, that value is conserved per subject, and that the
# per-subject anchor-bucket ordering holds throughout (a later preparation layer
# is never actionable until its predecessor layer has mined).
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

# Three parties with distinct, faithful balances. The whale is many notes so its
# preparation fans across several dependent layers, exercising the anchor-bucket
# ordering concurrently with the two single-layer migrations.
# Amounts chosen so each party's balance decomposes fully into 1-2-5 crossings
# (a sub-dust residual): the whale's ten 12-ZEC notes force a multi-layer
# preparation, and the three together stay under the single shielded coinbase the
# faucet holds (~137 ZEC).
PARTIES = [
    ("small holder", [Decimal('2')]),
    ("retail", [Decimal('12')]),
    ("whale", [Decimal('12')] * 10),
]


class ConcurrentMigrationScenario(IronwoodMigrationScenario):

    NUM_SUBJECTS = 3

    def run_test(self):
        self.sync_all()
        if self.skip_if_rpcs_absent(self.subject(0)):
            return

        # Fund each subject with its exact balance from the shared faucet.
        info = []
        for idx, (label, notes) in enumerate(PARTIES):
            before = self.fund_exact_orchard(idx, notes)
            expected_zat = int(sum(notes) * COIN)
            assert_equal(before, expected_zat,
                         "{} funded with exactly {} ZEC".format(
                             label, sum(notes)))
            print("  {}: {} note(s), {} zat.".format(
                label, len(notes), before))
            info.append({'label': label, 'acct': self.subject_account(idx),
                         'before': before, 'idx': idx})

        self.activate_ironwood()

        # Preview and start every subject's migration.
        subjects = []
        for d in info:
            subject = self.subject(d['idx'])
            pv = self.preview(subject, d['acct'])
            d['crossings'] = pv['funding_note_count']
            d['layers'] = pv['preparation']['layer_count']
            started = self.start(subject, d['acct'])
            d['migration_id'] = started['migration_id']
            d['total_txs'] = started['plan']['transaction_count']
            subjects.append((subject, d['acct'], d['migration_id']))
            print("  started {}: {} layer(s), {} crossings, {} txs.".format(
                d['label'], d['layers'], d['crossings'], d['total_txs']))
        assert_true(any(d['layers'] > 1 for d in info),
                    "at least one party (the whale) must be multi-layer to "
                    "exercise concurrent anchor-bucket ordering")

        # Drive all three concurrently, asserting each subject's next-actions
        # surface stays consistent at every step (the anchor-bucket ordering is
        # checked per snapshot inside assert_next_actions_consistent).
        def on_step(step, idx, adv):
            self.assert_next_actions_consistent(
                self.subject(info[idx]['idx']), info[idx]['migration_id'])

        print("Driving all {} migrations concurrently...".format(len(subjects)))
        done = self.drive_many_to_completion(subjects, on_step=on_step)
        assert_true(all(done),
                    "every concurrent migration completed within {} steps: {}"
                    .format(self.MAX_ADVANCE_STEPS,
                            {info[i]['label']: done[i] for i in range(len(done))}))

        # ---- assert the EXACT per-subject balances INLINE (self-contained) ---
        for d in info:
            subject = self.subject(d['idx'])
            notes = ironwood_notes(subject)
            note_values = sorted(int(n['valueZat']) for n in notes)
            ironwood_zat = sum(note_values)
            orchard_after = account_spendable_zat(subject, d['acct'],
                                                  Pool.ORCHARD)
            fees = d['before'] - ironwood_zat - orchard_after
            print("  [{}] source={} zat  Ironwood={} = {} zat  residual={} "
                  "fees={} over {} txs".format(
                      d['label'], d['before'], note_values, ironwood_zat,
                      orchard_after, fees, d['total_txs']))
            assert_equal(len(note_values), d['crossings'],
                         "{}: one Ironwood note per crossing".format(d['label']))
            assert_true(all(v > 0 for v in note_values),
                        "{}: every Ironwood note is positive".format(d['label']))
            assert_true(ironwood_zat + orchard_after <= d['before'],
                        "{}: no value was created".format(d['label']))
            assert_true(0 <= fees <= d['total_txs'] * 200000,
                        "{}: fees {} within bound".format(d['label'], fees))
            assert_true(orchard_after < 1000000,
                        "{}: only a sub-dust Orchard residual remains".format(
                            d['label']))
        print("\nConcurrent multi-party migration scenario passed.")


if __name__ == '__main__':
    ConcurrentMigrationScenario().main()
