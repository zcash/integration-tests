#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario B1/B2 + C1: the multi-layer anchor-bucket invariant and the mobile
# wallet's machine-readable "what do I sign next" surface.
#
# A many-note Orchard balance fans its preparation out across several dependent
# layers (multi-layer preparation is driven by the source note count: many notes
# must be consolidated across layers). Because a later layer spends the feeder
# notes an earlier layer mints, and those feeders are witnessable only once that
# earlier layer is mined, each layer must be signed and broadcast in a DIFFERENT
# anchor bucket: layer N is never actionable until its whole predecessor layer
# (layer N-1) has mined.
#
# The migration STATE MACHINE that enforces this lives in the librustzcash engine
# (a mobile wallet drives it directly); this scenario asserts that behavior end to
# end through zallet's regtest RPCs. A faucet funds the subject with many exact
# notes. At EVERY advance step it checks the per-transaction status surface for
# internal consistency and for the anchor-bucket ordering.
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

# Many source notes force a multi-layer preparation (the engine fans the
# consolidation out across dependent layers). Ten notes yields several layers.
SOURCE_NOTES = [Decimal('12')] * 10
SOURCE_ZAT = int(sum(SOURCE_NOTES) * COIN)


class NextActionsScenario(IronwoodMigrationScenario):

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
        preview = self.preview(subject, sacct)
        layers = preview['preparation']['layer_count']
        expected_crossings = preview['funding_note_count']
        print("  preview: {} layer(s), {} funding note(s).".format(
            layers, expected_crossings))
        assert_true(layers > 1,
                    "this scenario needs a multi-layer preparation to exercise "
                    "the anchor-bucket ordering; got {} layer(s)".format(layers))

        started = self.start(subject, sacct)
        migration_id = started['migration_id']
        total_txs = started['plan']['transaction_count']
        print("  started: id={!r} transaction_count={}".format(
            migration_id, total_txs))

        # The freshly started migration must already expose a consistent
        # next-actions surface: layer 0 ready to broadcast, later layers blocked
        # on their dependencies.
        st = self.assert_next_actions_consistent(subject, migration_id)
        ready0 = [t for t in st['transactions'] if t['ready']]
        assert_true(len(ready0) > 0,
                    "at least one layer-0 transaction is ready at start")
        assert_true(all(t['kind'] == 'preparation' and t['layer'] == 0
                        for t in ready0),
                    "only layer-0 preparation transactions are ready at start")
        blocked_later = [t for t in st['transactions']
                         if t['kind'] == 'preparation' and t['layer'] > 0]
        assert_true(all(t['blocked_on'] == 'dependencies' for t in blocked_later),
                    "later preparation layers start blocked on dependencies")
        print("  start surface OK: {} ready (layer 0), {} later-layer "
              "transactions blocked on dependencies.".format(
                  len(ready0), len(blocked_later)))

        # Drive to completion, asserting the surface's consistency and the
        # anchor-bucket ordering (both readiness and build order) at every step.
        def on_step(step, adv):
            self.assert_next_actions_consistent(subject, migration_id)
            if step % 20 == 0 or adv['phase'] == 'completed':
                print("  step {}: phase={} {}/{} - {}".format(
                    step, adv['phase'],
                    adv['progress']['completed_transactions'],
                    adv['progress']['total_transactions'], adv['status']))

        print("Driving the migration, asserting the next-actions surface each "
              "step...")
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
                    "only a sub-dust residual remains in Orchard: {} zat".format(
                        orchard_after))
        print("  Anchor-bucket ordering held across all layers. OK")
        print("\nNext-actions / anchor-bucket scenario passed.")


if __name__ == '__main__':
    NextActionsScenario().main()
