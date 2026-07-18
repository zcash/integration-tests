#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""Shared harness for the Orchard -> Ironwood pool-migration scenario suite.

Every migration scenario is a small test file that subclasses
`IronwoodMigrationScenario` and implements `run_test`, using the helpers here to
fund a particular wallet shape, drive the migration, and assert the invariants.
The migration STATE MACHINE lives in the librustzcash engine
(`zcash_pool_migration_backend`); the mobile wallet drives it directly and
zallet is a thin consumer, so these scenarios exercise shared-engine behavior
through zallet's regtest JSON-RPC.

Assertion helpers cover the cross-cutting invariants (value conservation, pool
exclusivity), the multi-layer ANCHOR-BUCKET ordering (a later preparation layer
is never actionable until its whole predecessor layer has mined), and the
machine-readable NEXT-ACTIONS surface a mobile wallet renders.
"""

from decimal import Decimal

from test_framework.util import (
    COIN,
    Pool,
    PrivacyPolicy,
    _RPC_EXCEPTIONS,
    account_spendable_zat,
    assert_true,
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
# mature coinbase) is mined before activation.
IRONWOOD_HEIGHT = 210

# The pool-migration RPC method names (the zallet interface is stable).
START_RPC = 'z_startpoolmigration'
STATUS_RPC = 'z_getpoolmigrationstatus'
ADVANCE_RPC = 'z_advancepoolmigration'
CANCEL_RPC = 'z_cancelpoolmigration'
LIST_RPC = 'z_listpoolmigrations'
PREVIEW_RPC = 'z_previewpoolmigration'

# Ironwood notes are Orchard-shaped, so the migration moves value from the
# Orchard pool into the Ironwood pool.
FROM_POOL = Pool.ORCHARD
TO_POOL = Pool.IRONWOOD

# JSON-RPC "method not found" code.
RPC_METHOD_NOT_FOUND = -32601


def orchard_notes(wallet, minconf=1):
    """The account's spendable Orchard notes."""
    return [u for u in wallet.z_listunspent(minconf)
            if u['pool'] == Pool.ORCHARD]


class IronwoodMigrationScenario(IronwoodTestFramework):
    """Base class for one migration scenario.

    Subclasses set any scenario-specific configuration and implement `run_test`,
    typically: fund a wallet shape, `preview`, `start`, `drive_to_completion` (or
    a partial drive for resume/cancel scenarios), then assert with the helpers.
    """

    # A generous cap: the migration advances one transaction per call, and the
    # transfers are spread across a randomized privacy schedule, so a wide bound
    # covers a many-note wallet with a spread schedule.
    MAX_ADVANCE_STEPS = 250
    # Blocks mined between advance steps: enough to confirm the just-broadcast
    # transaction and move the tip past each transfer's scheduled height.
    ADVANCE_MINE_BLOCKS = 16

    def __init__(self):
        super().__init__()
        # Deferred activation gives an Orchard era in which to mint the source
        # notes the migration consumes.
        self.activation_heights = nu_activation_ironwood_at(IRONWOOD_HEIGHT)

    # ---- capability probing ------------------------------------------------

    @staticmethod
    def _is_method_not_found(e):
        msg = str(e.error.get('message', '')).lower()
        return (e.error.get('code') == RPC_METHOD_NOT_FOUND
                or 'method not found' in msg)

    def migration_rpcs_present(self, w):
        """Whether the pool-migration RPC surface is wired in this build. Probes
        `start` with deliberately invalid arguments: an unknown method rejects
        the CALL (method-not-found); a wired method rejects the ARGUMENTS."""
        try:
            getattr(w, START_RPC)('__invalid_pool__', '__invalid_pool__')
        except _RPC_EXCEPTIONS as e:
            return not self._is_method_not_found(e)
        return True

    def skip_if_rpcs_absent(self, w):
        """Self-skip (clean exit) if the migration RPCs are not wired yet."""
        if not self.migration_rpcs_present(w):
            print("SKIP: pool-migration RPCs ({}) not wired in this build."
                  .format(START_RPC))
            return True
        return False

    # ---- funding wallet shapes ---------------------------------------------

    def _orchard_ua(self, w, acct):
        return w.z_getaddressforaccount(acct, ['orchard'])['address']

    def _sapling_ua(self, w, acct):
        return w.z_getaddressforaccount(acct, ['sapling'])['address']

    def fund_orchard_notes(self, node, w, taddr, acct, amounts_zec):
        """Mint one spendable v2 Orchard note per amount in `amounts_zec`
        (Decimal or number, in ZEC), all pre-NU6.3, and return the account's
        total spendable Orchard value in zat.

        A direct many-UTXO Orchard shield can be left unspendable by fee
        estimation, so shield into Sapling first, then pay the Orchard receiver
        once per note (sequential sends, so each mint is a distinct note and no
        single transaction has a duplicated recipient).
        """
        amounts = [Decimal(a) for a in amounts_zec]
        ua = self._orchard_ua(w, acct)
        sapling_ua = self._sapling_ua(w, acct)

        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "must still be in the Orchard era before funding")
        # A per-note fee buffer so the Sapling shield covers every send.
        needed = sum(amounts) + Decimal('0.001') * len(amounts)
        _, sapling_zat = shield_coinbase(
            node, w, taddr, sapling_ua, acct, Pool.SAPLING)
        assert_true(Decimal(sapling_zat) > needed * COIN,
                    "shielded enough Sapling to fund {} source note(s)"
                    .format(len(amounts)))

        for amount in amounts:
            opid = w.z_sendmany(
                sapling_ua, [{'address': ua, 'amount': amount}], 1, None,
                PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
            txid = wait_and_assert_operationid_status(w, opid)
            assert_true(txid is not None, "Sapling -> Orchard send should succeed")
            node.generate(1)
            wait_for_tx_scanned(w, txid)
            assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                        "the Orchard notes must be minted before NU6.3 activates")

        wait_for_account_spendable(w, acct, Pool.ORCHARD, min_zat=1)
        wait_account_settled(w, acct)
        orchard_zat = account_spendable_zat(w, acct, Pool.ORCHARD)
        assert_true(orchard_zat > 0, "the Orchard notes should be spendable")
        assert_true(len(orchard_notes(w)) >= len(amounts),
                    "expected {} Orchard source note(s)".format(len(amounts)))
        assert_true(len(ironwood_notes(w)) == 0,
                    "no Ironwood notes should exist before migration")
        return orchard_zat

    def fund_orchard_note(self, node, w, taddr, acct, zec):
        """Mint a single Orchard source note of `zec` ZEC (the common case)."""
        return self.fund_orchard_notes(node, w, taddr, acct, [zec])

    def activate_ironwood(self, node):
        """Advance the chain past the NU6.3 activation height so the migration is
        allowed (the source notes must already be minted)."""
        while node.getblockcount() < IRONWOOD_HEIGHT:
            node.generate(1)
        self.sync_all()

    # ---- lifecycle ----------------------------------------------------------

    def preview(self, w, acct):
        """The migration plan preview (read-only)."""
        return getattr(w, PREVIEW_RPC)(acct, FROM_POOL, TO_POOL)

    def start(self, w, acct):
        """Start (commit) the migration; returns the start response."""
        return getattr(w, START_RPC)(acct, FROM_POOL, TO_POOL)

    def status(self, w, migration_id):
        """The full migration status, including the per-transaction view."""
        return getattr(w, STATUS_RPC)(migration_id)

    def advance(self, w, acct, migration_id):
        """Advance the migration one step."""
        return getattr(w, ADVANCE_RPC)(acct, migration_id)

    def drive_to_completion(self, w, node, acct, migration_id,
                            on_step=None):
        """Advance one step per call, mining and syncing between steps, until the
        migration completes or the step cap is reached. `on_step(step, adv)` is
        called after each advance (for per-step assertions). Returns True if the
        migration completed."""
        for step in range(self.MAX_ADVANCE_STEPS):
            adv = self.advance(w, acct, migration_id)
            if on_step is not None:
                on_step(step, adv)
            if adv['phase'] == 'completed':
                return True
            node.generate(self.ADVANCE_MINE_BLOCKS)
            self.sync_all()
            wait_account_settled(w, acct)
        return False

    # ---- assertions: cross-cutting invariants ------------------------------

    def assert_value_crossed(self, w, acct, orchard_before):
        """After a completed migration, Ironwood notes exist and the Orchard
        balance has drained. (Denomination shape is deliberately not pinned.)"""
        ironwood = ironwood_notes(w)
        assert_true(len(ironwood) > 0, "the migration produced Ironwood notes")
        orchard_after = account_spendable_zat(w, acct, Pool.ORCHARD)
        assert_true(orchard_after < orchard_before,
                    "the Orchard balance drained as value crossed to Ironwood")
        return ironwood, orchard_after

    # ---- assertions: the next-actions / anchor-bucket surface --------------

    def assert_next_actions_consistent(self, w, migration_id):
        """The machine-readable status surface must be internally consistent: a
        `ready` transaction carries an `action` and no `blocked_on`; a waiting
        (planned/signed/proved) one carries a `blocked_on` and no `action`; a
        transaction blocked on its dependencies must have an unmined dependency;
        and a preparation transaction of layer > 0 is never ready while any
        transaction of its predecessor layer is unmined (the anchor-bucket
        ordering the mobile wallet relies on)."""
        st = self.status(w, migration_id)
        txs = st['transactions']
        by_id = {t['id']: t for t in txs}
        for t in txs:
            if t['ready']:
                assert_true(t.get('action') is not None,
                            "a ready transaction must name an action: {}".format(t))
                assert_true(t.get('blocked_on') is None,
                            "a ready transaction is not blocked: {}".format(t))
            elif t['state'] in ('planned', 'signed', 'proved', 'expired'):
                assert_true(t.get('blocked_on') is not None,
                            "a waiting transaction must report a blocker: {}"
                            .format(t))
                assert_true(t.get('action') is None,
                            "a waiting transaction has no action: {}".format(t))
            if t.get('blocked_on') == 'dependencies':
                unmined = [d for d in t['depends_on']
                           if by_id[d]['state'] != 'mined']
                assert_true(len(unmined) > 0,
                            "a dependency-blocked transaction must have an "
                            "unmined dependency: {}".format(t))
        # Anchor-bucket ordering: no layer-N (>0) transaction is ready while any
        # layer-(N-1) preparation transaction is unmined.
        prep = [t for t in txs if t['kind'] == 'preparation']
        layers = sorted({t['layer'] for t in prep})
        for layer in layers:
            if layer == 0:
                continue
            prior_unmined = any(
                t['state'] != 'mined' for t in prep if t['layer'] == layer - 1)
            if prior_unmined:
                for t in prep:
                    if t['layer'] == layer:
                        assert_true(not t['ready'],
                                    "layer {} must not be ready while layer {} "
                                    "is unmined: {}".format(layer, layer - 1, t))
        return st

    def assert_layers_signed_in_separate_buckets(self, seen_states):
        """Given the sequence of per-layer states observed across the drive,
        assert every later preparation layer only became `signed` AFTER its
        predecessor layer was fully `mined` (distinct anchor buckets). `seen_states`
        is the list of status snapshots captured per step."""
        # For each layer > 0, find the first step at which any of its transactions
        # was `signed`, and assert that at the prior step every predecessor-layer
        # transaction was already `mined`.
        for snap_prev, snap in zip(seen_states, seen_states[1:]):
            prev_by_layer = self._prep_by_layer(snap_prev)
            cur_by_layer = self._prep_by_layer(snap)
            for layer, txs in cur_by_layer.items():
                if layer == 0:
                    continue
                became_signed = any(
                    t['state'] in ('signed', 'proved') for t in txs) and all(
                    self._was_planned(layer, t['id'], snap_prev) for t in txs
                    if t['state'] in ('signed', 'proved'))
                if became_signed:
                    prior = prev_by_layer.get(layer - 1, [])
                    assert_true(prior and all(
                        t['state'] == 'mined' for t in prior),
                        "layer {} was signed before layer {} fully mined"
                        .format(layer, layer - 1))

    @staticmethod
    def _prep_by_layer(snapshot):
        out = {}
        for t in snapshot['transactions']:
            if t['kind'] == 'preparation':
                out.setdefault(t['layer'], []).append(t)
        return out

    @staticmethod
    def _was_planned(layer, tx_id, snapshot):
        for t in snapshot['transactions']:
            if t['id'] == tx_id:
                return t['state'] == 'planned'
        return False
