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
# still-unreleased Ironwood PCZT builder and therefore stays a stub; see
# wallet_ironwood_migration.py), the preview is fully wired: it reads the
# account's spendable Orchard balance and runs the librustzcash note-split
# planner (`zcash_ironwood_migration_backend::note_splitting`) to return the
# proposed decomposition. Nothing is scheduled, built, proved, or broadcast.
#
# Staging (mirrors wallet_ironwood_migration.py):
#
#   * Stage 0 -- method ABSENT: the preview method does not exist in the running
#     zallet build. run_test probes for it and SELF-SKIPS (returns cleanly, exit
#     0). This is where a zallet build predating the preview RPC lands.
#   * Stage 1 -- method PRESENT: assert the full planning contract. Because the
#     preview needs no engine build path, this is a REAL end-to-end assertion,
#     not a stub check: fund a v2 Orchard note pre-NU6.3, cross the activation
#     boundary, then assert the returned plan conserves value and reports sane
#     crossing denominations. Input validation (unsupported pool pair, unknown
#     strategy) is asserted too.
#
# The denomination POLICY ({1,2,5}*10^k ZEC vs the canonical powers of ten, and
# the sub-0.01 residual) is deliberately NOT pinned here; only conservation-style
# invariants are asserted, so this test survives strategy tuning.
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

# A strategy name no build supports, used to assert strategy validation.
UNSUPPORTED_STRATEGY = 'nonexistent_strategy'

# The ZIP-317 minimum fee (zat) the preview reserves as the note-split ("prep")
# fee. The preview documents this reserve; the engine computes the real prep fee
# once the build path lands.
ZIP317_MINIMUM_FEE_ZAT = 10000

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
        expected keys and internally-consistent counts."""
        assert_true(isinstance(plan, dict),
                    "preview should return a plan-summary object; got "
                    "{!r}".format(plan))
        for key in ('from_pool', 'to_pool', 'strategy', 'account_balance_zat',
                    'prep_fee_zat', 'total_input_zat', 'total_migratable_zat',
                    'source_change_zat', 'note_count', 'notes'):
            assert_true(key in plan,
                        "preview plan missing key {!r}; got keys {}".format(
                            key, sorted(plan.keys())))
        assert_equal(FROM_POOL, plan['from_pool'], "from_pool echoed")
        assert_equal(TO_POOL, plan['to_pool'], "to_pool echoed")
        assert_equal(len(plan['notes']), plan['note_count'],
                     "note_count matches the number of notes")

    def assert_conservation(self, plan, orchard_zat):
        """The plan conserves value: the prepared notes, the residual left in
        the source pool, and the reserved prep fee sum to the input balance; the
        migratable total is the sum of the crossing values; and every crossing
        is a positive value its note fully funds."""
        assert_equal(orchard_zat, plan['account_balance_zat'],
                     "preview reports the account's spendable Orchard balance")
        assert_equal(orchard_zat, plan['total_input_zat'],
                     "the plan decomposes the full spendable balance")
        assert_equal(ZIP317_MINIMUM_FEE_ZAT, plan['prep_fee_zat'],
                     "the preview reserves the ZIP-317 minimum prep fee")

        notes = plan['notes']
        crossings_sum = sum(n['crossing_zat'] for n in notes)
        outputs_sum = sum(n['output_zat'] for n in notes)
        for n in notes:
            assert_true(n['crossing_zat'] > 0,
                        "each crossing value is positive")
            assert_true(n['output_zat'] >= n['crossing_zat'],
                        "each prepared note fully funds its crossing value")

        assert_equal(crossings_sum, plan['total_migratable_zat'],
                     "total_migratable is the sum of the crossing values")
        assert_equal(
            orchard_zat,
            outputs_sum + plan['source_change_zat'] + plan['prep_fee_zat'],
            "notes + residual + prep fee conserve the input balance")

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
        e = expect_rpc_error(
            preview, acct, FROM_POOL, TO_POOL, 1, UNSUPPORTED_STRATEGY)
        print("  unknown strategy rejected: {!r}. OK".format(
            e.error.get('message', '')))

        # Stage 1b: the default (randomized) strategy plans and conserves value.
        print("Previewing the Orchard -> Ironwood plan (default strategy)...")
        plan = preview(acct, FROM_POOL, TO_POOL)
        self.assert_plan_shape(plan)
        assert_true(plan['note_count'] > 0,
                    "a funded account should produce at least one note")
        assert_true(plan['total_migratable_zat'] > 0,
                    "a funded account should migrate a positive amount")
        self.assert_conservation(plan, orchard_zat)
        print("  default plan: note_count={} migratable={} change={}. OK"
              .format(plan['note_count'], plan['total_migratable_zat'],
                      plan['source_change_zat']))

        # Stage 1c: the canonical strategy is also selectable and conserves
        # value (the decomposition differs; only invariants are asserted).
        print("Previewing with the canonical strategy...")
        canonical = preview(acct, FROM_POOL, TO_POOL, 1, 'canonical')
        self.assert_plan_shape(canonical)
        assert_equal('canonical', canonical['strategy'],
                     "the canonical strategy is echoed back")
        self.assert_conservation(canonical, orchard_zat)
        print("  canonical plan: note_count={} migratable={}. OK".format(
            canonical['note_count'], canonical['total_migratable_zat']))

        print("\nIronwood migration preview checks passed.")


if __name__ == '__main__':
    WalletIronwoodMigrationPreviewTest().main()
