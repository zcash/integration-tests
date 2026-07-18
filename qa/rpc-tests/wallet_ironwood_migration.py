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

        # The start method validates the pool pair, which requires NU6.3 to be
        # active, before returning its scaffold response. Cross the activation
        # boundary so the surface checks reach that response rather than an
        # "upgrade not active" rejection. No funded notes are needed for the
        # surface; the funded Stage-2 flow will instead fund pre-activation and
        # cross the boundary afterward.
        to_activation = max(0, IRONWOOD_HEIGHT - node.getblockcount())
        if to_activation > 0:
            node.generate(to_activation)
            self.sync_all()
        assert_true(node.getblockcount() >= IRONWOOD_HEIGHT,
                    "NU6.3 must be active for the migration surface")

        # Stage 1: assert the RPC surface (callable + input validation) without
        # the engine. These do not need funded notes.
        print("Stage 1: assert the pool-migration RPC surface...")
        self.assert_start_callable(w, start_name)
        self.assert_input_validation(w, start_name)
        self.assert_companion_surface(w, status_name, list_name)
        print("  RPC surface OK.")

        # ---- Stage 2 TODO(end-to-end): drive a real migration --------------
        # Once the engine's commit/broadcast slices are wired into zallet (the
        # start method actually builds, pre-signs, persists, and broadcasts the
        # PCZTs), replace this block with the funded lifecycle scenarios below.
        # Each is a distinct real-world case; the fixture (`fund_orchard_source`)
        # and the boundary crossing are already in place:
        #     acct = w.z_listaccounts()[0]['account_uuid']
        #     orchard_zat = self.fund_orchard_source(node, w, taddr, acct)
        #     node.generate(max(0, IRONWOOD_HEIGHT - node.getblockcount()))
        #     self.sync_all()
        #
        # (a) FULL LIFECYCLE: start the migration, then advance/poll with
        #     `advance_name`/`status_name`, confirming and scanning each produced
        #     transaction (the preparation first, then each scheduled transfer),
        #     until status reports complete. Assert only stable invariants (NOT
        #     the denomination policy, still evolving):
        #       * the Orchard pool drains to (near) zero,
        #       * the Ironwood pool gains the migrated value (minus fees),
        #       * value is conserved across the two pools,
        #       * each produced Ironwood note is a planned denomination,
        #       * the amounts that cross match what `z_previewpoolmigration`
        #         promised for the same balance (ZIP-318 consent).
        # (b) RESUME AFTER RESTART: after the preparation is mined but before the
        #     transfers complete, restart the wallet (stop/start the node's
        #     wallet), then assert the migration is still present in
        #     `status_name`/`list_name` (the store round-trips) and that
        #     advancing continues it to completion. A migration must survive the
        #     wallet being closed mid-run.
        # (c) STATUS / LIST REFLECT PROGRESS: assert `status_name` moves through
        #     its lifecycle (planned -> in progress -> complete) as transactions
        #     mine, and that `list_name` shows exactly the one in-progress
        #     migration while it runs and none once it completes.
        # (d) EXPIRY / REORG: let a scheduled transfer pass its expiry height
        #     without mining and assert it is rebuilt (not lost); invalidate the
        #     block that mined the preparation (a short reorg) and assert the
        #     migration recovers rather than double-spending or stalling.
        assert node is not None  # `node`/`taddr` drive the Stage-2 flow above
        assert taddr is not None
        print("TODO: drive the funded Orchard -> Ironwood lifecycle (full run, "
              "resume-after-restart, status/list progress, expiry/reorg) once "
              "the engine's commit/broadcast slices are wired into zallet.")

        print("\nIronwood migration RPC-surface checks passed.")


if __name__ == '__main__':
    WalletIronwoodMigrationTest().main()
