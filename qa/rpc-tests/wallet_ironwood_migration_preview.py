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
#     the value invariants below. It also covers the real-world cases a caller
#     hits: realistic bad requests (a pool migrated to itself, the
#     reverse/unsupported direction, an unsupported source pool, a nonexistent
#     account, and an unknown pool name) are REJECTED; a minconf larger than any
#     note's confirmations leaves NOTHING to migrate; and the preview is
#     SIDE-EFFECT-FREE (no funds move, no Ironwood notes are minted).
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

# ZIP 318's denomination cap: no crossing exceeds DENOM_CAP ZEC, keeping a
# whale's crossings within the shared denomination set.
DENOM_CAP_ZEC = 10_000
DENOM_CAP_ZAT = DENOM_CAP_ZEC * COIN

# A confirmation depth larger than any regtest chain height. Requiring it
# excludes every spendable note, so a caller who asks for more confirmations
# than a recent note has must see "nothing to migrate", never a stale or partial
# plan. Exercises the preview's minconf filter and its empty-balance path.
UNREACHABLE_MINCONF = 1_000_000

# A well-formed but nonexistent account id, for the invalid-account rejection.
NONEXISTENT_ACCOUNT = '00000000-0000-0000-0000-000000000000'


def is_canonical_denomination(zat):
    """True if `zat` is a canonical ZIP-318 crossing value: a {1, 2, 5} * 10^k
    amount in zatoshi. The split mints denominations from the cap down to a
    sub-1-ZEC dust floor, so a crossing need not be a whole number of ZEC
    (e.g. 0.5 ZEC = 50_000_000 zat is canonical); this checks the {1,2,5}*10^k
    shape directly in zatoshi."""
    if zat <= 0:
        return False
    v = zat
    while v % 10 == 0:
        v //= 10
    return v in (1, 2, 5)

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
        receiver (mirrors wallet_ironwood_migration.py). The engine then splits
        this note into the several funding notes/crossings the plan asserts,
        which is the common real-world case (a wallet consolidates, then
        migrates)."""
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

    def assert_denomination_structure(self, plan):
        """ZIP-318 denomination invariants: every crossing in the raw note split
        is a canonical {1,2,5}*10^k whole-ZEC value within the cap, the split is
        non-increasing (a descending greedy decomposition summing to its
        migratable total), and each scheduled funding note is a canonical
        denomination too (the funding notes are the reconciled subset)."""
        split = plan['note_split']['crossing_values']
        assert_true(len(split) > 0, "a funded account yields a non-empty split")
        for v in split:
            assert_true(is_canonical_denomination(v),
                        "note-split crossing {} zat is a canonical "
                        "{{1,2,5}}*10^k ZEC denomination".format(v))
            assert_true(v <= DENOM_CAP_ZAT,
                        "note-split crossing {} zat within the {}-ZEC cap"
                        .format(v, DENOM_CAP_ZEC))
        assert_equal(split, sorted(split, reverse=True),
                     "the raw note split is non-increasing (descending greedy)")
        assert_equal(sum(split), plan['note_split']['total_migratable_zat'],
                     "the split's crossings sum to its migratable total")
        for n in plan['funding_notes']:
            assert_true(is_canonical_denomination(n['crossing_zat']),
                        "each scheduled funding note is a canonical denomination")

    def assert_rejects_invalid_requests(self, preview, acct):
        """Realistic caller mistakes the preview must reject with an RPC error
        (never a plan): migrating a pool to itself, an unsupported migration
        direction, a source pool with no wired migration, and a nonexistent
        account. The pool-pair checks run before the account is looked up, so
        they use the real account; the account check uses a valid pool pair so
        the account is what fails."""
        # Migrating a pool into itself is meaningless.
        e = expect_rpc_error(preview, acct, FROM_POOL, FROM_POOL)
        print("  same-pool rejected: {!r}. OK".format(
            e.error.get('message', '')))
        # The reverse direction (Ironwood -> Orchard) is not a supported
        # migration; only Orchard -> Ironwood is wired.
        e = expect_rpc_error(preview, acct, TO_POOL, FROM_POOL)
        print("  reverse direction rejected: {!r}. OK".format(
            e.error.get('message', '')))
        # Sapling is a valid pool name but has no wired migration to Ironwood.
        e = expect_rpc_error(preview, acct, Pool.SAPLING, TO_POOL)
        print("  unsupported source pool rejected: {!r}. OK".format(
            e.error.get('message', '')))
        # A nonexistent account (valid pool pair, so the account is what the
        # request turns on): it is rejected -- whether because the id does not
        # resolve or because it holds no spendable source-pool balance, a caller
        # who names the wrong account gets a clean error, never a plan.
        e = expect_rpc_error(preview, NONEXISTENT_ACCOUNT, FROM_POOL, TO_POOL)
        print("  nonexistent account rejected: {!r}. OK".format(
            e.error.get('message', '')))

    def assert_high_minconf_has_nothing_to_migrate(self, preview, acct):
        """A confirmation requirement no recent note can meet leaves nothing
        spendable, so a funded account reports nothing to migrate rather than a
        stale plan. Exercises the preview's minconf filter and empty-balance
        path, the case a caller hits by demanding more confirmations than the
        note has."""
        e = expect_rpc_error(
            preview, acct, FROM_POOL, TO_POOL, UNREACHABLE_MINCONF)
        print("  unreachable minconf yields nothing to migrate: {!r}. OK"
              .format(e.error.get('message', '')))

    def assert_preview_is_read_only(self, w, acct, before_orchard_zat):
        """The preview must be side-effect-free: after previewing (and the
        rejected requests), the account's spendable Orchard balance is unchanged
        and no Ironwood notes have been minted. A user can preview freely
        without any funds crossing the turnstile."""
        assert_equal(
            before_orchard_zat, account_spendable_zat(w, acct, Pool.ORCHARD),
            "previewing does not change the Orchard balance")
        assert_true(len(ironwood_notes(w)) == 0,
                    "previewing mints no Ironwood notes")

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

        # Stage 1a: input validation -- an unknown pool name, plus realistic
        # caller mistakes (same pool, reverse direction, unsupported source,
        # nonexistent account).
        print("Asserting preview input validation...")
        e = expect_rpc_error(preview, acct, UNSUPPORTED_POOL, TO_POOL)
        print("  unknown pool name rejected: {!r}. OK".format(
            e.error.get('message', '')))
        self.assert_rejects_invalid_requests(preview, acct)

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
        self.assert_denomination_structure(plan)
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

        # Determinism of SHAPE: the denomination decomposition is a function of
        # the balance, so a second preview yields the same multiset of crossings
        # (only the randomized transfer schedule may differ, not the split).
        print("Re-previewing to assert the denomination shape is deterministic...")
        plan2 = preview(acct, FROM_POOL, TO_POOL)
        assert_equal(sorted(plan['note_split']['crossing_values']),
                     sorted(plan2['note_split']['crossing_values']),
                     "the denomination shape is stable across previews")
        print("  denominations stable across two previews. OK")

        # A confirmation requirement no recent note can meet: the funded account
        # still reports nothing to migrate (the minconf filter + empty-balance
        # path), rather than a stale plan.
        print("Asserting an unreachable minconf yields nothing to migrate...")
        self.assert_high_minconf_has_nothing_to_migrate(preview, acct)

        # Everything above (every preview and every rejected request) must have
        # been side-effect-free: no funds moved, no Ironwood notes minted.
        print("Asserting previewing moved no funds...")
        self.assert_preview_is_read_only(w, acct, orchard_zat)
        print("  balance unchanged and no Ironwood notes. OK")

        print("\nIronwood migration preview checks passed.")


if __name__ == '__main__':
    WalletIronwoodMigrationPreviewTest().main()
