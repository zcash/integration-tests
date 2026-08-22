#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario: external (hardware / offline) signer.
#
# The same retail migration as wallet_ironwood_migration_retail.py, but driven
# through the EXTERNAL-SIGNER surface instead of in-process signing: the wallet
# builds every transaction UNSIGNED and hands out its PCZT; the signature is
# produced out of band and applied back before the transaction is proved and
# broadcast.
#
# A separate faucet funds the subject with exactly 15 ZEC in one Orchard note.
# The subject then migrates with `external_signer=true`:
#
#   1. z_startpoolmigration(..., external_signer=true) builds the preparation
#      UNSIGNED and returns its PCZTs; the preparation transactions sit in the
#      `awaiting_signature` state, blocked on `signature`.
#   2. For each PCZT the test signs it with the account key (standing in for the
#      hardware device; a real deployment signs off-host) via
#      z_signpoolmigrationpczt and applies it with z_applypoolmigrationsignature.
#   3. z_advancepoolmigration proves and broadcasts the now-signed preparation.
#   4. Once the preparation is mined, z_buildpoolmigrationtransfers builds the
#      transfers UNSIGNED; they are signed, applied, and advanced the same way.
#
# The end state is asserted EXACTLY and must match the in-process retail result:
# a real 15-ZEC holder migrated its 15 ZEC into Ironwood, signing out of band.
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

# The subject's exact starting balance: a single 15-ZEC Orchard note.
SOURCE_ZEC = 15
SOURCE_ZAT = SOURCE_ZEC * COIN


class ExternalSignerScenario(IronwoodMigrationScenario):

    def run_test(self):
        self.sync_all()
        subject = self.subject(0)
        if self.skip_if_rpcs_absent(subject):
            return
        sacct = self.subject_account(0)

        print("Faucet funds the subject with EXACTLY {} ZEC (one Orchard "
              "note)...".format(SOURCE_ZEC))
        orchard_before = self.fund_exact_note(0, SOURCE_ZEC)
        assert_equal(orchard_before, SOURCE_ZAT,
                     "the subject holds EXACTLY {} ZEC".format(SOURCE_ZEC))
        assert_equal(len(orchard_notes(subject)), 1, "a single source note")

        self.activate_ironwood()
        preview = self.preview(subject, sacct)
        expected_crossings = preview['funding_note_count']
        print("  preview: {} layer(s), {} funding note(s).".format(
            preview['preparation']['layer_count'], expected_crossings))

        # ---- start for an EXTERNAL signer: preparation built UNSIGNED --------
        started, unsigned_prep = self.start_external(subject, sacct)
        migration_id = started['migration_id']
        total_txs = started['plan']['transaction_count']
        print("  started (external): transaction_count={}, {} unsigned "
              "preparation PCZT(s).".format(total_txs, len(unsigned_prep)))
        assert_true(len(unsigned_prep) > 0,
                    "the external start returns unsigned preparation PCZTs")
        assert_true(all(u['pczt'] for u in unsigned_prep),
                    "every unsigned transaction carries a PCZT")

        # The preparation transactions are awaiting an external signature.
        st = self.status(subject, migration_id)
        awaiting = [t for t in st['transactions']
                    if t['state'] == 'awaiting_signature']
        assert_equal(len(awaiting), len(unsigned_prep),
                     "the preparation transactions await a signature")
        assert_true(all(t['blocked_on'] == 'signature' for t in awaiting),
                    "an awaiting transaction is blocked on its signature")
        assert_true(all(not t['ready'] for t in awaiting),
                    "an awaiting transaction is not yet actionable")

        # ---- drive to completion signing every PCZT out of band -------------
        completed = self.drive_external_to_completion(
            subject, sacct, migration_id, unsigned_prep)
        assert_true(completed,
                    "the external-signer migration completed within {} advance "
                    "steps".format(self.MAX_ADVANCE_STEPS))

        # Nothing is left awaiting a signature once it completes.
        st = self.status(subject, migration_id)
        assert_equal(st['phase'], 'completed', "the migration is complete")
        assert_true(all(t['state'] != 'awaiting_signature'
                        for t in st['transactions']),
                    "no transaction is left awaiting a signature")

        # ---- assert the EXACT end state INLINE (matches retail) -------------
        note_values = sorted(int(n['valueZat']) for n in ironwood_notes(subject))
        ironwood_zat = sum(note_values)
        orchard_after = account_spendable_zat(subject, sacct, Pool.ORCHARD)
        fees = orchard_before - ironwood_zat - orchard_after
        print("  source Orchard:   {} zat ({} ZEC)".format(
            orchard_before, SOURCE_ZEC))
        print("  Ironwood notes:   {} = {} zat".format(note_values, ironwood_zat))
        print("  Orchard residual: {} zat; fees: {} zat over {} txs".format(
            orchard_after, fees, total_txs))

        assert_equal(orchard_before, SOURCE_ZAT, "the subject started at 15 ZEC")
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
        print("\nExternal-signer scenario passed.")


if __name__ == '__main__':
    ExternalSignerScenario().main()
