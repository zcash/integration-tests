#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario E2: crossing denomination shape.
#
# The migration crosses value in canonical 1-2-5 denominations (each crossing is
# d * 10^k ZEC for d in {1, 2, 5}), which is what gives the on-chain footprint
# its intended anonymity. This scenario migrates a balance and asserts inline
# that every Ironwood note value, reduced by its factors of ten, is 1, 2, or 5,
# in addition to the usual conservation checks.
#

from test_framework.ironwood_migration_common import IronwoodMigrationScenario
from test_framework.util import (
    Pool,
    account_spendable_zat,
    assert_equal,
    assert_true,
    ironwood_notes,
)

SOURCE_NOTE_ZEC = 60


def _leading_digit(value_zat):
    """The significant digit of a value once its trailing zeros are removed; for
    a 1-2-5 * 10^k denomination this is 1, 2, or 5."""
    v = int(value_zat)
    while v % 10 == 0:
        v //= 10
    return v


class DenominationsScenario(IronwoodMigrationScenario):

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        if self.skip_if_rpcs_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']

        print("Funding an Orchard balance ({} ZEC nominal)...".format(
            SOURCE_NOTE_ZEC))
        orchard_before = self.fund_orchard_note(node, w, taddr, acct,
                                                SOURCE_NOTE_ZEC)
        self.activate_ironwood(node)

        preview = self.preview(w, acct)
        expected_crossings = preview['funding_note_count']
        print("  preview: {} layer(s), {} funding note(s).".format(
            preview['preparation']['layer_count'], expected_crossings))

        started = self.start(w, acct)
        migration_id = started['migration_id']
        total_txs = started['plan']['transaction_count']

        completed = self.drive_to_completion(w, node, acct, migration_id)
        assert_true(completed,
                    "the migration completed within {} advance steps".format(
                        self.MAX_ADVANCE_STEPS))

        # ---- assert the exact balances and denominations INLINE -------------
        notes = ironwood_notes(w)
        note_values = sorted(int(n['valueZat']) for n in notes)
        ironwood_zat = sum(note_values)
        orchard_after = account_spendable_zat(w, acct, Pool.ORCHARD)
        fees = orchard_before - ironwood_zat - orchard_after
        digits = sorted({_leading_digit(v) for v in note_values})
        print("  source Orchard:   {} zat".format(orchard_before))
        print("  Ironwood notes:   {} = {} zat".format(note_values, ironwood_zat))
        print("  leading digits:   {}".format(digits))
        print("  Orchard residual: {} zat; fees: {} zat over {} txs".format(
            orchard_after, fees, total_txs))

        # Every Ironwood note is a canonical 1-2-5 denomination.
        assert_true(all(_leading_digit(v) in (1, 2, 5) for v in note_values),
                    "every Ironwood note is a 1-2-5 denomination: {}".format(
                        note_values))
        # One Ironwood note per crossing; value conserved with a bounded fee.
        assert_equal(len(note_values), expected_crossings,
                     "one Ironwood note per crossing")
        assert_true(ironwood_zat + orchard_after <= orchard_before,
                    "no value was created")
        assert_true(0 <= fees <= total_txs * 200000,
                    "fees {} zat within bound".format(fees))
        assert_true(orchard_after < 1000000,
                    "sub-dust Orchard residual: {} zat".format(orchard_after))
        print("\nDenominations scenario passed.")


if __name__ == '__main__':
    DenominationsScenario().main()
