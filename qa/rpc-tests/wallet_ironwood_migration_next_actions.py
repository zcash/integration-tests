#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario B1/B2 + C1: the multi-layer anchor-bucket invariant and the mobile
# wallet's machine-readable "what do I sign next" surface.
#
# A large Orchard balance fans its preparation out across several dependent
# layers. Because a later layer spends the feeder notes an earlier layer mints,
# and those feeders are witnessable only once that earlier layer is mined, each
# layer must be signed and broadcast in a DIFFERENT anchor bucket: layer N is
# never actionable until its whole predecessor layer (layer N-1) has mined.
#
# The migration STATE MACHINE that enforces this lives in the librustzcash
# engine (a mobile wallet drives it directly); this scenario asserts that
# behavior end to end through zallet's regtest RPCs. At EVERY advance step it
# checks the per-transaction status surface for internal consistency and for the
# anchor-bucket ordering, and it records the per-layer state sequence to confirm
# no later layer was signed before its predecessor fully mined.
#

from test_framework.ironwood_migration_common import IronwoodMigrationScenario
from test_framework.util import assert_true

# A balance large enough to force a multi-layer preparation (the engine fans a
# whale out across dependent layers). The exact layer count is a function of the
# planner; the scenario only requires more than one layer.
SOURCE_NOTE_ZEC = 78


class NextActionsScenario(IronwoodMigrationScenario):

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        # Bring the wallet up to the chain tip so account RPCs are accepted.
        self.sync_all()

        if self.skip_if_rpcs_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']

        print("Funding a whale Orchard source ({} ZEC)...".format(
            SOURCE_NOTE_ZEC))
        orchard_before = self.fund_orchard_note(node, w, taddr, acct,
                                                 SOURCE_NOTE_ZEC)
        self.activate_ironwood(node)

        preview = self.preview(w, acct)
        layers = preview['preparation']['layer_count']
        print("  preview: {} layer(s), {} funding note(s).".format(
            layers, preview['funding_note_count']))
        assert_true(layers > 1,
                    "this scenario needs a multi-layer preparation to exercise "
                    "the anchor-bucket ordering; got {} layer(s)".format(layers))

        started = self.start(w, acct)
        migration_id = started['migration_id']
        print("  started: id={!r} transaction_count={}".format(
            migration_id, started['plan']['transaction_count']))

        # The freshly started migration must already expose a consistent
        # next-actions surface: layer 0 ready to broadcast, later layers blocked
        # on their dependencies.
        st = self.assert_next_actions_consistent(w, migration_id)
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
            self.assert_next_actions_consistent(w, migration_id)
            if step % 20 == 0 or adv['phase'] == 'completed':
                print("  step {}: phase={} {}/{} - {}".format(
                    step, adv['phase'],
                    adv['progress']['completed_transactions'],
                    adv['progress']['total_transactions'], adv['status']))

        print("Driving the migration, asserting the next-actions surface each "
              "step...")
        completed = self.drive_to_completion(w, node, acct, migration_id,
                                             on_step=on_step)
        assert_true(completed,
                    "the migration completed within {} advance steps".format(
                        self.MAX_ADVANCE_STEPS))

        ironwood, orchard_after = self.assert_value_crossed(w, acct,
                                                            orchard_before)
        print("  {} Ironwood note(s); Orchard {} -> {} zat. Anchor-bucket "
              "ordering held across all layers. OK".format(
                  len(ironwood), orchard_before, orchard_after))
        print("\nNext-actions / anchor-bucket scenario passed.")


if __name__ == '__main__':
    NextActionsScenario().main()
