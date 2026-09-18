#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario D3: cancel a migration mid-flight.
#
# After some of a migration's transactions have been broadcast, cancelling it
# must be honored: the migration becomes terminal, it is not resurrected by a
# later advance, and no crash or dangling in-progress state is left behind. A
# wallet UI's "cancel" must always be a clean exit. A faucet funds the subject
# with an exact balance.
#

from test_framework.ironwood_migration_common import (
    ADVANCE_RPC,
    CANCEL_RPC,
    IronwoodMigrationScenario,
)
from test_framework.util import _RPC_EXCEPTIONS, assert_true

SOURCE_ZEC = 8
# Advance a few steps before cancelling, to broadcast part of layer 0.
STEPS_BEFORE_CANCEL = 3


class CancelScenario(IronwoodMigrationScenario):

    def run_test(self):
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)
        node = self.faucet_node

        print("Faucet funds the subject with EXACTLY {} ZEC...".format(
            SOURCE_ZEC))
        self.fund_exact_note(0, SOURCE_ZEC)
        self.activate_ironwood()

        started = self.start(subject, sacct)
        migration_id = started['migration_id']
        print("  started: id={!r}.".format(migration_id))

        # Advance a few steps to broadcast some of the preparation. Sync mempools
        # before mining so the subject's broadcast transactions reach the miner.
        for _ in range(STEPS_BEFORE_CANCEL):
            adv = self.advance(subject, sacct, migration_id)
            if adv['phase'] == 'completed':
                break
            self.sync_all()
            node.generate(self.ADVANCE_MINE_BLOCKS)
            self.sync_all()
        print("  advanced {} step(s); now cancelling.".format(
            STEPS_BEFORE_CANCEL))

        # Cancel must succeed.
        getattr(subject, CANCEL_RPC)(migration_id)

        # The migration is now terminal: its status is neither in progress nor
        # completed (a cancelled migration), or the status RPC rejects the id.
        try:
            st = self.status(subject, migration_id)
        except _RPC_EXCEPTIONS as e:
            print("  status after cancel rejects the id (terminal): {!r}. OK"
                  .format(str(e.error.get('message', ''))))
        else:
            phase = st['phase']
            assert_true(phase not in ('in_progress', 'completed'),
                        "a cancelled migration is terminal, not in-progress or "
                        "completed; got phase={!r}".format(phase))
            print("  status after cancel: phase={!r} (terminal). OK".format(
                phase))

        # A later advance must not resurrect the migration.
        try:
            adv = getattr(subject, ADVANCE_RPC)(sacct, migration_id)
        except _RPC_EXCEPTIONS as e:
            print("  advance after cancel rejected (not resurrected): {!r}. OK"
                  .format(str(e.error.get('message', ''))))
        else:
            assert_true(adv['phase'] not in ('in_progress',),
                        "advance must not resurrect a cancelled migration; got "
                        "phase={!r}".format(adv['phase']))
            print("  advance after cancel is a no-op (phase={!r}). OK".format(
                adv['phase']))

        print("\nCancel scenario passed.")


if __name__ == '__main__':
    CancelScenario().main()
