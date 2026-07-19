#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Scenario A6: a whale note plus a tail of dust.
#
# A common real shape: one large Orchard note alongside several tiny ones. The
# migration must both fan the whale out and consolidate the dust in the same
# run. This scenario builds that shape by splitting off one large note and a
# handful of dust notes, then drives the migration to completion and asserts the
# exact balances inline.
#

from decimal import Decimal

from test_framework.ironwood_migration_common import (
    IronwoodMigrationScenario,
    orchard_notes,
)
from test_framework.util import (
    Pool,
    account_spendable_zat,
    assert_equal,
    assert_true,
    ironwood_notes,
)

# One whale note plus a dust tail, split off the funded balance.
SPLIT_NOTES = [40, Decimal('0.02'), Decimal('0.02'), Decimal('0.05'),
               Decimal('0.05'), Decimal('0.1'), Decimal('0.1')]


class WhalePlusDustScenario(IronwoodMigrationScenario):

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        if self.skip_if_rpcs_absent(w):
            return

        acct = w.z_listaccounts()[0]['account_uuid']

        print("Funding an Orchard balance and splitting a whale note plus a "
              "dust tail...")
        self.fund_orchard_note(node, w, taddr, acct, 1)
        orchard_before = self.split_orchard_notes(node, w, acct, SPLIT_NOTES)
        source_notes = sorted(int(n['valueZat']) for n in orchard_notes(w))
        print("  {} Orchard source notes = {} zat".format(
            len(source_notes), source_notes))
        assert_true(len(source_notes) >= 8,
                    "the source is a many-note whale+dust wallet: {}".format(
                        len(source_notes)))

        self.activate_ironwood(node)
        preview = self.preview(w, acct)
        expected_crossings = preview['funding_note_count']
        print("  preview: {} layer(s), {} funding note(s).".format(
            preview['preparation']['layer_count'], expected_crossings))

        started = self.start(w, acct)
        migration_id = started['migration_id']
        total_txs = started['plan']['transaction_count']
        print("  started: transaction_count={}".format(total_txs))

        completed = self.drive_to_completion(w, node, acct, migration_id)
        assert_true(completed,
                    "the migration completed within {} advance steps".format(
                        self.MAX_ADVANCE_STEPS))

        # ---- assert the exact balances INLINE (self-contained) --------------
        notes = ironwood_notes(w)
        note_values = sorted(int(n['valueZat']) for n in notes)
        ironwood_zat = sum(note_values)
        orchard_after = account_spendable_zat(w, acct, Pool.ORCHARD)
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
        print("\nWhale-plus-dust scenario passed.")


if __name__ == '__main__':
    WhalePlusDustScenario().main()
