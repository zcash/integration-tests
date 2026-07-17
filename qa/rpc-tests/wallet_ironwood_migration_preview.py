#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Orchard -> Ironwood migration PLANNING PREVIEW (NU6.3, ZIP 2005), against the
# Z3 stack (zebrad + zaino + zallet).
#
# This exercises `z_previewpoolmigration`: the read-only planning slice of the
# generic pool-migration surface. Unlike `z_startpoolmigration` (which needs the
# still-unreleased engine commit/reconcile slices that build, pre-sign, and
# persist the PCZTs, and therefore stays a stub; see wallet_ironwood_migration.py),
# the preview is fully wired: it enumerates the account's spendable Orchard notes
# and runs the librustzcash migration engine's planning slice
# (`zcash_pool_migration_backend::engine::plan_migration`) to return the proposed
# plan. Nothing is scheduled, built, proved, or broadcast.
#
# Staging (mirrors wallet_ironwood_migration.py):
#
#   * Stage 0 -- method ABSENT: the preview method does not exist in the running
#     zallet build. run_test probes for it and SELF-SKIPS (returns cleanly, exit
#     0). This is where a zallet build predating the preview RPC lands.
#   * Stage 1 -- method PRESENT: assert the full planning contract. Because the
#     preview needs no engine build path, this is a REAL end-to-end assertion,
#     not a stub check: fund a v2 Orchard note pre-NU6.3, cross the activation
#     boundary, then assert the returned plan reports sane crossing denominations,
#     a transfer schedule (a broadcast height and expiry per funding note), and
#     the value invariants below. Input validation (an unsupported pool pair) is
#     asserted too.
#
# The denomination POLICY (the {1,2,5}*10^k ZEC quantization and the sub-0.01
# residual) is deliberately NOT pinned here; only structural and conservation-
# style invariants are asserted, so this test survives strategy tuning.
#
# Registered in NEW_SCRIPTS but listed in DISABLED_SCRIPTS in
# qa/pull-tester/rpc-tests.py until a zallet build exposing
# `z_previewpoolmigration` is the pinned one; the Stage-0 self-skip keeps it safe
# to run against older builds in the meantime.
#

from decimal import Decimal

from test_framework.util import (
    COIN,
    Pool,
    PrivacyPolicy,
    _RPC_EXCEPTIONS,
    account_spendable_zat,
    assert_equal,
    assert_true,
    expect_rpc_error,
    ironwood_notes,
    nu_activation_ironwood_at,
    shield_coinbase,
    wait_account_settled,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
)
from test_framework.util_ironwood import IronwoodTestFramework

# Defer NU6.3 past coinbase maturity so the Orchard-era shield (which needs
# mature coinbase) is mined before activation. Mirrors
# wallet_ironwood_activation.py / wallet_ironwood_migration.py.
IRONWOOD_HEIGHT = 210

# JSON-RPC "method not found" code; an unknown method returns this before it
# parses any argument.
RPC_METHOD_NOT_FOUND = -32601

# The generic migration is driven by pool names. Ironwood notes are
# Orchard-shaped, so the migration moves value from the Orchard pool into the
# Ironwood pool.
FROM_POOL = Pool.ORCHARD
TO_POOL = Pool.IRONWOOD

# A pool name no build supports, used to assert input validation independently
# of which migration directions are wired.
UNSUPPORTED_POOL = 'nonexistent_pool'

# The ZIP-317 fee (zat) the preview reserves per note-preparation transaction:
# the fixed padded action count times the marginal fee (PREP_TX_ACTIONS = 16,
# MARGINAL_FEE = 5000 zat). The engine and the preview derive this from the
# crate's own constants; it is reproduced here to assert the reported value.
PREP_TX_ACTIONS = 16
MARGINAL_FEE_ZAT = 5000
PREP_FEE_ZAT = PREP_TX_ACTIONS * MARGINAL_FEE_ZAT

# The per-note fee buffer (zat) each funding note carries on top of its crossing
# value so it self-funds its migration transfer: two source-pool plus two
# destination-pool actions at the marginal fee (4 * MARGINAL_FEE).
TRANSFER_FEE_BUFFER_ZAT = 4 * MARGINAL_FEE_ZAT

# Candidate names for the preview method. The exact name may be adjusted to
# zallet conventions, so probe a small list and take the first that resolves.
PREVIEW_RPC_CANDIDATES = (
    'z_previewpoolmigration',
    'z_preview_pool_migration',
    'z_poolmigrationpreview',
    'z_migratepoolpreview',
)


class WalletIronwoodMigrationPreviewTest(IronwoodTestFramework):

    def __init__(self):
        super().__init__()
        # Deferred activation gives an Orchard era in which to mint the v2
        # Orchard note the preview plans over.
        self.activation_heights = nu_activation_ironwood_at(IRONWOOD_HEIGHT)

    # ---- capability probing ------------------------------------------------

    @staticmethod
    def _is_method_not_found(e):
        msg = str(e.error.get('message', '')).lower()
        return (e.error.get('code') == RPC_METHOD_NOT_FOUND
                or 'method not found' in msg)

    def resolve_rpc_name(self, w, candidates, *probe_args):
        """Return the first candidate name that resolves to a real method, else
        None. Probes with `probe_args` (deliberately invalid, so the probe can
        never trigger real work): an unknown method rejects the CALL
        (method-not-found); an existing method rejects the ARGUMENTS."""
        for name in candidates:
            method = getattr(w, name)
            try:
                method(*probe_args)
            except _RPC_EXCEPTIONS as e:
                if self._is_method_not_found(e):
                    continue  # this candidate does not exist; try the next
                return name  # exists (rejected the arguments, not the method)
            else:
                return name  # exists and (surprisingly) accepted the probe
        return None

    # ---- fixture -----------------------------------------------------------

    def fund_orchard_source(self, node, w, taddr, acct):
        """Mint a single spendable v2 Orchard note pre-NU6.3 -- the migration
        source the preview plans over -- and return its spendable value in zat.
        A direct many-UTXO Orchard shield can be left unspendable by fee
        estimation, so shield into Sapling first, then pay the Orchard-only
        receiver (mirrors wallet_ironwood_migration.py)."""
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']
        sapling_ua = w.z_getaddressforaccount(acct, ['sapling'])['address']

        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "must still be in the Orchard era before funding")
        _, sapling_zat = shield_coinbase(
            node, w, taddr, sapling_ua, acct, Pool.SAPLING)
        orch_target = (Decimal(sapling_zat) / COIN / 2).quantize(
            Decimal('0.0001'))
        opid = w.z_sendmany(
            sapling_ua, [{'address': ua, 'amount': orch_target}], 1, None,
            PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
        txid = wait_and_assert_operationid_status(w, opid)
        assert_true(txid is not None, "Sapling -> Orchard send should succeed")
        node.generate(1)
        wait_for_tx_scanned(w, txid)
        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "the Orchard note must be minted before NU6.3 activates")
        wait_for_account_spendable(w, acct, Pool.ORCHARD, min_zat=1)
        wait_account_settled(w, acct)
        orchard_zat = account_spendable_zat(w, acct, Pool.ORCHARD)
        assert_true(orchard_zat > 0, "the Orchard note should be spendable")
        assert_true(len(ironwood_notes(w)) == 0,
                    "no Ironwood notes should exist before migration")
        return orchard_zat

    # ---- assertions --------------------------------------------------------

    @staticmethod
    def assert_plan_shape(plan):
        """A preview response is a structured plan-summary object with the
        expected keys, nested funding-note and summary objects, and
        internally-consistent counts."""
        assert_true(isinstance(plan, dict),
                    "preview should return a plan-summary object; got "
                    "{!r}".format(plan))
        for key in ('from_pool', 'to_pool', 'enabling_upgrade',
                    'account_balance_zat', 'prep_fee_zat',
                    'total_migratable_zat', 'source_change_zat',
                    'funding_note_count', 'funding_notes', 'note_split',
                    'preparation'):
            assert_true(key in plan,
                        "preview plan missing key {!r}; got keys {}".format(
                            key, sorted(plan.keys())))
        assert_equal(FROM_POOL, plan['from_pool'], "from_pool echoed")
        assert_equal(TO_POOL, plan['to_pool'], "to_pool echoed")
        assert_equal(len(plan['funding_notes']), plan['funding_note_count'],
                     "funding_note_count matches the number of funding notes")

        # Each funding note carries its crossing value, its self-funding output,
        # and a transfer schedule.
        for note in plan['funding_notes']:
            for key in ('output_zat', 'crossing_zat', 'broadcast_height',
                        'expiry_height'):
                assert_true(key in note,
                            "funding note missing key {!r}; got {}".format(
                                key, sorted(note.keys())))

        # The note-split and preparation summaries have their own shapes.
        for key in ('note_count', 'total_migratable_zat', 'crossing_values'):
            assert_true(key in plan['note_split'],
                        "note_split missing key {!r}".format(key))
        for key in ('layer_count', 'transaction_count', 'residual_note_count'):
            assert_true(key in plan['preparation'],
                        "preparation missing key {!r}".format(key))

    def assert_conservation(self, plan, orchard_zat):
        """The plan's value invariants: it reports the account's spendable
        source-pool balance, reserves the padded ZIP-317 prep fee, and each
        funding note has a positive crossing that its output covers by exactly
        the fixed transfer fee buffer. The migratable total is the sum of the
        crossings and never exceeds the balance."""
        assert_equal(orchard_zat, plan['account_balance_zat'],
                     "preview reports the account's spendable Orchard balance")
        assert_equal(PREP_FEE_ZAT, plan['prep_fee_zat'],
                     "the preview reserves the padded ZIP-317 prep fee")

        notes = plan['funding_notes']
        crossings_sum = sum(n['crossing_zat'] for n in notes)
        for n in notes:
            assert_true(n['crossing_zat'] > 0,
                        "each crossing value is positive")
            assert_equal(n['crossing_zat'] + TRANSFER_FEE_BUFFER_ZAT,
                         n['output_zat'],
                         "each output funds the crossing plus the fee buffer")
            assert_true(n['expiry_height'] > n['broadcast_height'],
                        "the transfer expiry follows its broadcast height")

        assert_equal(crossings_sum, plan['total_migratable_zat'],
                     "total_migratable is the sum of the crossing values")
        assert_true(plan['total_migratable_zat'] <= plan['account_balance_zat'],
                    "the migratable value never exceeds the input balance")
        # Reconciliation only ever drops funding notes, so at most as many as the
        # raw note split proposed.
        assert_true(len(notes) <= plan['note_split']['note_count'],
                    "funding notes are a subset of the raw split")

    # ---- driver ------------------------------------------------------------

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        self.sync_all()
        acct = w.z_listaccounts()[0]['account_uuid']

        # Stage 0 gate: skip cleanly until the running zallet exposes the preview
        # method. The probe uses an unsupported pool pair so it can never do real
        # work, only reveal whether the method resolves.
        preview_name = self.resolve_rpc_name(
            w, PREVIEW_RPC_CANDIDATES, acct, UNSUPPORTED_POOL, UNSUPPORTED_POOL)
        if preview_name is None:
            print("SKIP: no zallet pool-migration preview RPC yet (tried {}); "
                  "scaffolding only.".format(", ".join(PREVIEW_RPC_CANDIDATES)))
            return
        preview = getattr(w, preview_name)
        print("Pool-migration preview RPC: {}".format(preview_name))

        # Fund the Orchard source in the Orchard era, then cross the NU6.3
        # boundary so the migration (and hence the preview) is enabled.
        print("Funding an Orchard source note pre-NU6.3...")
        orchard_zat = self.fund_orchard_source(node, w, taddr, acct)
        print("  Orchard source spendable: {} zat.".format(orchard_zat))

        to_activation = max(0, IRONWOOD_HEIGHT - node.getblockcount())
        if to_activation > 0:
            node.generate(to_activation)
        self.sync_all()
        assert_true(node.getblockcount() >= IRONWOOD_HEIGHT,
                    "NU6.3 must be active before previewing the migration")
        # The pre-NU6.3 note stays an Orchard note (the migration source); it is
        # not reclassified as Ironwood.
        assert_equal(orchard_zat,
                     account_spendable_zat(w, acct, Pool.ORCHARD),
                     "the Orchard source note survives NU6.3 activation")

        # Stage 1a: input validation.
        print("Asserting preview input validation...")
        e = expect_rpc_error(preview, acct, UNSUPPORTED_POOL, TO_POOL)
        print("  unsupported pool pair rejected: {!r}. OK".format(
            e.error.get('message', '')))

        # Stage 1b: the migration plans, is scheduled, and conserves value.
        print("Previewing the Orchard -> Ironwood plan...")
        plan = preview(acct, FROM_POOL, TO_POOL)
        self.assert_plan_shape(plan)
        assert_true(plan['funding_note_count'] > 0,
                    "a funded account should produce at least one funding note")
        assert_true(plan['total_migratable_zat'] > 0,
                    "a funded account should migrate a positive amount")
        assert_equal(len(plan['funding_notes']), plan['funding_note_count'],
                     "one schedule entry per funding note")
        self.assert_conservation(plan, orchard_zat)
        print("  plan: funding_notes={} migratable={} change={} "
              "layers={} txs={}. OK".format(
                  plan['funding_note_count'], plan['total_migratable_zat'],
                  plan['source_change_zat'],
                  plan['preparation']['layer_count'],
                  plan['preparation']['transaction_count']))

        # Every scheduled transfer is broadcast at or after the current tip.
        tip = node.getblockcount()
        for n in plan['funding_notes']:
            assert_true(n['broadcast_height'] >= tip - 1,
                        "a transfer is scheduled at or after the chain tip")

        print("\nIronwood migration preview checks passed.")


if __name__ == '__main__':
    WalletIronwoodMigrationPreviewTest().main()
