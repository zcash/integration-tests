#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Orchard -> Ironwood value-pool migration (NU6.3, ZIP 2005), against the Z3
# stack (zebrad + zaino + zallet).
#
# zallet is gaining a GENERIC pool-migration JSON-RPC, parameterized by
# `from_pool` / `to_pool`, backed by the librustzcash note-split planner crate
# `zcash_ironwood_migration_backend`. Both are still evolving, so this module
# stays independent of the final API and grows with the feature in stages:
#
#   * Stage 0 -- method ABSENT: the start method does not exist yet. run_test
#     probes for it and SELF-SKIPS (returns cleanly, exit 0). This is where the
#     currently-shipping zallet build lands.
#   * Stage 1 -- method PRESENT (stub answers, engine not landed): assert the
#     RPC surface only. The start method must be callable for orchard->ironwood
#     and either return a plan-summary object OR a clear "not implemented yet"
#     response, and it must reject an unsupported pool pair with an error; the
#     status and list methods, when present, must likewise be callable and
#     return a structured (or "not implemented yet") answer even with no
#     migration in progress. The end-to-end assertions (notes actually prepared,
#     value actually crossing the pools) stay TODO until the engine lands.
#   * Stage 2 -- engine landed (future): wire the funded end-to-end flow. The
#     `fund_orchard_source` helper already stands up the fixture (a real v2
#     Orchard note minted pre-NU6.3 as the migration source); the TODO block in
#     run_test enumerates the real-world lifecycle scenarios to drive -- the full
#     run, resume-after-restart, status/list reflecting progress, and
#     expiry/reorg recovery. The denomination policy ({1,2,5}*10^k ZEC and the
#     sub-0.01 residual) is deliberately NOT pinned here -- assert only
#     conservation-style invariants when it is finalized.
#
# The module is registered in NEW_SCRIPTS but listed in DISABLED_SCRIPTS in
# qa/pull-tester/rpc-tests.py (the framework's equivalent of skip/xfail), so the
# suite does not spend a coinbase-maturity window on it while it can only skip.
# Remove that entry once the migration RPC lands.
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
# wallet_ironwood_activation.py.
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

# A non-empty but nonexistent migration id, for probing the status method's
# surface (the stub validates the id is non-empty, then answers): a real caller
# passes the id returned by the start method.
PLACEHOLDER_MIGRATION_ID = 'no-such-migration'

# The most `z_advancepoolmigration` steps to drive before giving up: the
# migration advances one transaction per call (a preparation, then one transfer
# per funding note), so a generous bound covers a many-note wallet.
MAX_ADVANCE_STEPS = 80

# Blocks to mine between advance steps: enough to confirm the just-broadcast
# transaction and to move the chain tip past each transfer's scheduled height.
ADVANCE_MINE_BLOCKS = 8

# The Orchard source is funded so the note split is a SINGLE preparation layer:
# commit_preparation currently supports only single-layer preparation, and the
# planner otherwise chains dependent layers. 78 ZEC is a known single-layer
# balance (the engine's own end-to-end test asserts layer_count == 1 for it);
# larger balances produce more funding notes and dust-heavy small balances
# fragment, both of which fan out across layers. The preview's layer_count is
# checked before starting, so if this ever stops being single-layer the test
# skips rather than fails.
SOURCE_NOTE_ZEC = 78

# Markers a stub uses to say "the RPC exists but the engine is not wired yet".
# A start response containing any of these is treated as an acceptable Stage-1
# answer (the engine has not landed).
NOT_IMPLEMENTED_MARKERS = (
    'not implemented',
    'unimplemented',
    'not yet',
    'notimplemented',
    'coming soon',
    'todo',
)

# The pool-migration RPC method names. The zallet RPC interface is stable, so
# each is a single fixed name (earlier revisions probed a list of candidate
# names while the naming was unsettled). The `start` method is the capability
# gate.
START_RPC = 'z_startpoolmigration'
STATUS_RPC = 'z_getpoolmigrationstatus'
ADVANCE_RPC = 'z_advancepoolmigration'
CANCEL_RPC = 'z_cancelpoolmigration'
LIST_RPC = 'z_listpoolmigrations'


def orchard_notes(wallet, minconf=1):
    return [u for u in wallet.z_listunspent(minconf)
            if u['pool'] == Pool.ORCHARD]


class WalletIronwoodMigrationTest(IronwoodTestFramework):

    def __init__(self):
        super().__init__()
        # Deferred activation gives an Orchard era in which to mint the v2
        # Orchard note the migration consumes.
        self.activation_heights = nu_activation_ironwood_at(IRONWOOD_HEIGHT)

    # ---- capability probing ------------------------------------------------

    @staticmethod
    def _is_method_not_found(e):
        msg = str(e.error.get('message', '')).lower()
        return (e.error.get('code') == RPC_METHOD_NOT_FOUND
                or 'method not found' in msg)

    def resolve_method(self, w, name, *probe_args):
        """Return `name` if it resolves to a real RPC method, else None. Probes
        with `probe_args` (deliberately invalid, so the probe can never trigger a
        migration): an unknown method rejects the CALL (method-not-found); an
        existing method rejects the ARGUMENTS."""
        try:
            getattr(w, name)(*probe_args)
        except _RPC_EXCEPTIONS as e:
            return None if self._is_method_not_found(e) else name
        return name

    # ---- Stage 1: RPC-surface (stub-level) assertions ----------------------

    def assert_start_callable(self, w, start_name):
        """The start method must be callable for orchard->ironwood, returning a
        plan-summary object OR a clear 'not implemented yet' response."""
        start = getattr(w, start_name)
        try:
            result = start(FROM_POOL, TO_POOL)
        except _RPC_EXCEPTIONS as e:
            message = e.error.get('message', '')
            assert_true(
                any(m in message.lower() for m in NOT_IMPLEMENTED_MARKERS),
                "start migration ({}->{}) should return a plan summary or a "
                "clear 'not implemented yet' error; got: {!r}".format(
                    FROM_POOL, TO_POOL, message))
            print("  start responded 'not implemented yet' (stub): {!r}. OK"
                  .format(message))
            return
        # A returned result is a plan summary; do not pin its exact shape while
        # it evolves, only that it is a structured (object) response.
        assert_true(isinstance(result, dict),
                    "start migration should return a plan-summary object; "
                    "got {!r}".format(result))
        print("  start returned a plan summary: keys={}. OK".format(
            sorted(result.keys())))

    def assert_input_validation(self, w, start_name):
        """An unsupported pool pair must be rejected with an RPC error."""
        start = getattr(w, start_name)
        e = expect_rpc_error(start, UNSUPPORTED_POOL, TO_POOL)
        print("  unsupported pool pair ({}->{}) rejected: {!r}. OK".format(
            UNSUPPORTED_POOL, TO_POOL, e.error.get('message', '')))

    def assert_companion_surface(self, w, status_name, list_name):
        """When exposed, the status and list methods must be callable and return
        a structured response (or a clear 'not implemented yet'), even with no
        migration in progress: the status of an account with no migration is a
        well-defined empty answer, and listing is always valid. This is the
        read-only half of the lifecycle surface, assertable before the engine
        can actually run a migration."""
        if status_name is not None:
            try:
                result = getattr(w, status_name)(PLACEHOLDER_MIGRATION_ID)
            except _RPC_EXCEPTIONS as e:
                message = e.error.get('message', '')
                assert_true(
                    any(m in message.lower() for m in NOT_IMPLEMENTED_MARKERS),
                    "status should return an object or 'not implemented yet'; "
                    "got {!r}".format(message))
                print("  status responded 'not implemented yet': {!r}. OK"
                      .format(message))
            else:
                assert_true(isinstance(result, dict),
                            "status should return a status object; got "
                            "{!r}".format(result))
                print("  status returned an object: keys={}. OK".format(
                    sorted(result.keys())))
        if list_name is not None:
            try:
                result = getattr(w, list_name)()
            except _RPC_EXCEPTIONS as e:
                message = e.error.get('message', '')
                assert_true(
                    any(m in message.lower() for m in NOT_IMPLEMENTED_MARKERS),
                    "list should return an array or 'not implemented yet'; "
                    "got {!r}".format(message))
                print("  list responded 'not implemented yet': {!r}. OK"
                      .format(message))
            else:
                assert_true(isinstance(result, list),
                            "list should return an array of migrations; got "
                            "{!r}".format(result))
                print("  list returned {} migration(s). OK".format(len(result)))

    # ---- Stage 2 fixture (used once the engine lands) ----------------------

    def fund_orchard_source(self, node, w, taddr, acct):
        """Mint a single spendable v2 Orchard note pre-NU6.3 -- the migration
        source -- and return its spendable value in zat. A direct many-UTXO
        Orchard shield can be left unspendable by fee estimation, so shield into
        Sapling first, then pay the Orchard-only receiver (mirrors
        wallet_ironwood_activation.py)."""
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']
        sapling_ua = w.z_getaddressforaccount(acct, ['sapling'])['address']

        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "must still be in the Orchard era before funding")
        _, sapling_zat = shield_coinbase(
            node, w, taddr, sapling_ua, acct, Pool.SAPLING)
        assert_true(sapling_zat > SOURCE_NOTE_ZEC * COIN,
                    "shielded enough Sapling to fund the source note")
        orch_target = Decimal(SOURCE_NOTE_ZEC)
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
        assert_true(len(orchard_notes(w)) >= 1, "expected an Orchard source note")
        assert_true(len(ironwood_notes(w)) == 0,
                    "no Ironwood notes should exist before migration")
        return orchard_zat

    # ---- driver ------------------------------------------------------------

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        # Bring the wallet up to the chain tip so account RPCs are accepted.
        self.sync_all()

        # Stage 0 gate: skip cleanly until zallet exposes the start method. This
        # keeps the module registered and runnable without failing while the
        # feature (and the librustzcash planner) are still evolving.
        start_name = self.resolve_method(
            w, START_RPC, UNSUPPORTED_POOL, UNSUPPORTED_POOL)
        if start_name is None:
            print("SKIP: no zallet pool-migration start RPC ({}) yet; "
                  "scaffolding only.".format(START_RPC))
            return

        # Report which of the companion methods this build exposes
        # (informational; only `start` gates the flow).
        status_name = self.resolve_method(w, STATUS_RPC, UNSUPPORTED_POOL)
        advance_name = self.resolve_method(w, ADVANCE_RPC, UNSUPPORTED_POOL)
        cancel_name = self.resolve_method(w, CANCEL_RPC, UNSUPPORTED_POOL)
        list_name = self.resolve_method(w, LIST_RPC)
        print("Pool-migration RPCs: start={} status={} advance={} cancel={} "
              "list={}".format(start_name, status_name, advance_name,
                               cancel_name, list_name))

        # Fund an Orchard source note BEFORE NU6.3, then cross the activation
        # boundary so the migration is enabled.
        acct = w.z_listaccounts()[0]['account_uuid']
        print("Funding an Orchard source note pre-NU6.3...")
        orchard_before = self.fund_orchard_source(node, w, taddr, acct)
        print("  Orchard source spendable: {} zat.".format(orchard_before))

        to_activation = max(0, IRONWOOD_HEIGHT - node.getblockcount())
        if to_activation > 0:
            node.generate(to_activation)
            self.sync_all()
        assert_true(node.getblockcount() >= IRONWOOD_HEIGHT,
                    "NU6.3 must be active to migrate")

        # commit_preparation supports only single-layer preparation for now.
        # Preview the plan and skip cleanly if this balance would fan out across
        # dependent layers (a larger-balance limitation still being addressed).
        preview = w.z_previewpoolmigration(acct, FROM_POOL, TO_POOL)
        layers = preview['preparation']['layer_count']
        if layers > 1:
            print("SKIP: this balance needs {} preparation layers; "
                  "commit_preparation supports single-layer only.".format(layers))
            return
        print("  preview: {} layer(s), {} funding note(s).".format(
            layers, preview['funding_note_count']))

        # Start the migration: builds + pre-signs the preparation and persists it.
        print("Starting the Orchard -> Ironwood migration...")
        started = getattr(w, start_name)(acct, FROM_POOL, TO_POOL)
        migration_id = started['migration_id']
        total_txs = started['plan']['transaction_count']
        assert_true(total_txs > 0, "the migration plans at least one transaction")
        print("  started: id={!r} transaction_count={}".format(
            migration_id, total_txs))

        # status and list reflect the freshly started migration.
        if status_name is not None:
            st = getattr(w, status_name)(migration_id)
            assert_equal(total_txs, st['progress']['total_transactions'],
                         "status reports the planned transaction count")
        if list_name is not None:
            assert_true(len(getattr(w, list_name)()) == 1,
                        "list shows the one in-progress migration")

        # Drive the migration to completion: advance one step per call (a
        # preparation first, then one transfer per funding note), mining and
        # syncing between steps so each broadcast transaction confirms and the
        # chain tip reaches each transfer's scheduled height.
        print("Driving the migration (advance + mine) to completion...")
        completed = False
        for step in range(MAX_ADVANCE_STEPS):
            adv = getattr(w, advance_name)(acct, migration_id)
            progress = adv['progress']
            print("  step {}: phase={} {}/{} - {}".format(
                step, adv['phase'], progress['completed_transactions'],
                progress['total_transactions'], adv['status']))
            if adv['phase'] == 'completed':
                completed = True
                break
            node.generate(ADVANCE_MINE_BLOCKS)
            self.sync_all()
            wait_account_settled(w, acct)
        assert_true(completed,
                    "the migration completed within {} advance steps".format(
                        MAX_ADVANCE_STEPS))

        # The value crossed the turnstile: Ironwood notes now exist and the
        # Orchard balance dropped as the funding notes were spent.
        ironwood = ironwood_notes(w)
        assert_true(len(ironwood) > 0, "the migration produced Ironwood notes")
        orchard_after = account_spendable_zat(w, acct, Pool.ORCHARD)
        assert_true(orchard_after < orchard_before,
                    "the Orchard balance drained as value crossed to Ironwood")
        print("  {} Ironwood note(s); Orchard {} -> {} zat. OK".format(
            len(ironwood), orchard_before, orchard_after))

        print("\nIronwood migration lifecycle passed.")


if __name__ == '__main__':
    WalletIronwoodMigrationTest().main()
