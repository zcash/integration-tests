#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario D13: status / advance / cancel on an unknown migration id.
#
# The read and drive methods must reject an id that does not name the wallet's
# migration, both when no migration exists and when one is in progress. A wallet
# that polls or resumes with a stale or wrong id must get a clear error, never a
# crash or a silent no-op that looks like success. A faucet funds the subject.
#

from test_framework.ironwood_migration_common import (
    ADVANCE_RPC,
    CANCEL_RPC,
    IronwoodMigrationScenario,
    STATUS_RPC,
)
from test_framework.util import _RPC_EXCEPTIONS, assert_true

UNKNOWN_ID = 'no-such-migration'


class UnknownIdScenario(IronwoodMigrationScenario):

    def _expect_rejected(self, w, acct, label):
        """status / advance / cancel on UNKNOWN_ID must all be rejected."""
        for name, call in (
            (STATUS_RPC, lambda: getattr(w, STATUS_RPC)(UNKNOWN_ID)),
            (ADVANCE_RPC, lambda: getattr(w, ADVANCE_RPC)(acct, UNKNOWN_ID)),
            (CANCEL_RPC, lambda: getattr(w, CANCEL_RPC)(UNKNOWN_ID)),
        ):
            try:
                call()
            except _RPC_EXCEPTIONS as e:
                print("  {} ({}) rejected: {!r}. OK".format(
                    name, label, str(e.error.get('message', ''))))
            else:
                raise AssertionError(
                    "{} must reject an unknown migration id ({})".format(
                        name, label))

    def run_test(self):
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)

        # (a) No migration exists yet: an unknown id is rejected.
        print("Probing an unknown id with no migration in progress...")
        self._expect_rejected(subject, sacct, "no migration in progress")

        # (b) A migration is in progress: an id that is not it is still rejected.
        print("Funding and starting a migration, then probing a wrong id...")
        self.fund_exact_note(0, 5)
        self.activate_ironwood()
        self.start(subject, sacct)
        self._expect_rejected(subject, sacct, "wrong id while one is in progress")

        # The real migration is still addressable by its own id.
        listed = getattr(subject, 'z_listpoolmigrations')()
        assert_true(len(listed) == 1,
                    "the real migration is unaffected by the wrong-id probes")
        print("\nUnknown-id scenario passed.")


if __name__ == '__main__':
    UnknownIdScenario().main()
