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
# wallet UI's "cancel" must always be a clean exit.
#

from test_framework.ironwood_migration_common import (
    ADVANCE_RPC,
    CANCEL_RPC,
    IronwoodMigrationScenario,
)
from test_framework.util import _RPC_EXCEPTIONS, assert_true

SOURCE_NOTE_ZEC = 8
# Advance a few steps before cancelling, to broadcast part of layer 0.
STEPS_BEFORE_CANCEL = 3


class CancelScenario(IronwoodMigrationScenario):

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        if self.skip_if_rpcs_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']

        print("Funding an Orchard source ({} ZEC)...".format(SOURCE_NOTE_ZEC))
        self.fund_orchard_note(node, w, taddr, acct, SOURCE_NOTE_ZEC)
        self.activate_ironwood(node)

        started = self.start(w, acct)
        migration_id = started['migration_id']
        print("  started: id={!r}.".format(migration_id))

        # Advance a few steps to broadcast some of the preparation.
        for _ in range(STEPS_BEFORE_CANCEL):
            adv = self.advance(w, acct, migration_id)
            if adv['phase'] == 'completed':
                break
            node.generate(self.ADVANCE_MINE_BLOCKS)
            self.sync_all()
        print("  advanced {} step(s); now cancelling.".format(
            STEPS_BEFORE_CANCEL))

        # Cancel must succeed.
        getattr(w, CANCEL_RPC)(migration_id)

        # The migration is now terminal: its status is neither in progress nor
        # completed (a cancelled migration), or the status RPC rejects the id.
        try:
            st = self.status(w, migration_id)
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
            adv = getattr(w, ADVANCE_RPC)(acct, migration_id)
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
