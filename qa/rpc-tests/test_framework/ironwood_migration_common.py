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

FAUCET model
------------
A regtest wallet that mines its own coinbase always ends up holding the WHOLE
shielded coinbase (~137 ZEC), because a shielded send routes its change back
into the account's preferred pool; the nominal "amount" cannot shrink it. To
give each subject a TRUTHFUL balance (a 2 ZEC small holder really holding 2 ZEC),
the harness separates the MINER from the SUBJECT: wallet 0 is a FAUCET that mines
and shields the coinbase, and wallets 1.. are SUBJECTS the faucet funds with
EXACT amounts. The faucet keeps the change, so a subject holds precisely what it
was sent. This also enables MULTIPLE subjects migrating concurrently on one
shared chain (`NUM_SUBJECTS` > 1, driven by `drive_many_to_completion`).

Assertion helpers cover the cross-cutting invariants (value conservation, pool
exclusivity), the multi-layer ANCHOR-BUCKET ordering (a later preparation layer
is never actionable until its whole predecessor layer has mined), and the
machine-readable NEXT-ACTIONS surface a mobile wallet renders.
"""

import time
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
    wait_for_account_spendable,
    wait_for_tx_scanned,
    wait_and_assert_operationid_status,
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
# The external-signer surface: build unsigned, sign out of band, apply the
# signature, then advance proves and broadcasts as usual.
BUILD_TRANSFERS_RPC = 'z_buildpoolmigrationtransfers'
SIGN_PCZT_RPC = 'z_signpoolmigrationpczt'
APPLY_SIGNATURE_RPC = 'z_applypoolmigrationsignature'

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

    Subclasses set `NUM_SUBJECTS` (default 1), implement `run_test`, and
    typically: fund each subject with an exact Orchard balance via the faucet
    (`fund_exact_orchard`), `preview`, `start`, `drive_to_completion` (or the
    concurrent `drive_many_to_completion`), then assert with the helpers.
    """

    # How many SUBJECT wallets the scenario migrates (in addition to the faucet
    # wallet 0). One faucet plus `NUM_SUBJECTS` subjects all share ONE node.
    NUM_SUBJECTS = 1

    # A generous cap: the migration advances one transaction per call, and the
    # transfers are spread across a randomized privacy schedule, so a wide bound
    # covers a many-note wallet with a spread schedule.
    MAX_ADVANCE_STEPS = 250
    # Blocks mined between advance steps: enough to confirm the just-broadcast
    # transaction and move the tip past each transfer's scheduled height.
    ADVANCE_MINE_BLOCKS = 16

    def __init__(self):
        super().__init__()
        # A SINGLE shared node hosts every wallet. The framework normally binds
        # one node per wallet, but multiple peered zebra regtest nodes do not
        # converge past two (multi-peer propagation bug, zebra #10329 / #10332).
        # Instead all wallets follow ONE node (node 0) and submit to its mempool,
        # so there is no inter-node peering to stall: wallet 0 is the faucet (and
        # node 0 mines to its address); wallets 1.. are subjects.
        self.num_nodes = 1
        self.num_wallets = 1 + self.NUM_SUBJECTS
        # Deferred activation gives an Orchard era in which to mint the source
        # notes the migration consumes.
        self.activation_heights = nu_activation_ironwood_at(IRONWOOD_HEIGHT)
        # Lazily shielded on first fund; the faucet's coinbase is shielded once.
        self._faucet_ready = False

    def setup_network(self, split=False, do_mempool_sync=True):
        """Single-node setup: start the one shared node, then start every wallet
        against it (see setup_wallets). A freshly started wallet does not report a
        committed wallet_tip until it has scanned a block, so mine one block and
        wait for every wallet to reach the node's tip before the scenario runs.
        There is no peering (one node), so no cross-node sync is involved."""
        self.prepare_wallets()
        self.nodes = self.setup_nodes()
        self.is_network_split = split
        self.prepare_chain()
        self.zainos = self.setup_indexers()
        self.wallets = self.setup_wallets()
        self.nodes[0].generate(1)
        self.sync_all()

    def sync_all(self, do_mempool_sync=True):
        """Wait until every wallet has committed the shared node's tip. The
        framework's `sync_blocks` cannot be used here: it compares a length-1
        node-tip list against a length-N wallet-tip list (it assumes one wallet
        per node), so it never matches when N wallets share one node, even though
        each wallet's tip does equal the node's. No mempool sync is needed either
        (every wallet submits to the single node's mempool, which mines it)."""
        node = self.nodes[0]
        deadline = time.time() + 90
        while time.time() < deadline:
            tip = node.getbestblockhash()
            synced = True
            for w in (self.wallets or []):
                try:
                    wt = w.getwalletstatus().get('wallet_tip')
                except _RPC_EXCEPTIONS:
                    synced = False
                    break
                if not wt or wt.get('blockhash') != tip:
                    synced = False
                    break
            if synced:
                return
            time.sleep(0.25)
        raise AssertionError("wallets did not reach the node tip")

    def setup_wallets(self):
        """Start every wallet against the SINGLE shared node (node 0), rather than
        the framework's default one-node-per-wallet binding. All wallets read node
        0's state (read-only) and submit to its validator RPC, so they share one
        chain and one mempool with no inter-node peering (which is what avoids the
        zebra regtest multi-peer bug). Node 0 mines every block and its mempool
        already holds each wallet's broadcast, so no cross-node propagation is
        needed. Each wallet keeps its OWN data directory and RPC port; only the
        validator connection (RPC, gRPC indexer, and read-only state path) is
        shared. The wallet databases were already created by `prepare_wallets`."""
        import subprocess

        from test_framework.config import ZalletArgs
        from test_framework.util import (
            get_rpc_auth_proxy,
            indexer_rpc_port,
            node_dir,
            rpc_port,
            rpc_url_wallet,
            update_zallet_conf,
            wait_for_wallet_start,
            wallet_dir,
            wallet_rpc_port,
            zallet_binary,
            zallet_processes,
        )

        dirname = self.options.tmpdir
        binary = zallet_binary()
        wallets = []
        for i in range(self.num_wallets):
            datadir = wallet_dir(dirname, i)
            zallet_args = ZalletArgs(activation_heights=self.activation_heights)
            # Point this wallet at node 0's validator RPC, gRPC indexer, and
            # read-only state directory (all wallets share node 0).
            update_zallet_conf(
                datadir, rpc_port(0), wallet_rpc_port(i), zallet_args,
                indexer_port=indexer_rpc_port(0),
                zebra_state_dir=node_dir(dirname, 0))
            zallet_processes[i] = subprocess.Popen(
                [binary, "-d=" + datadir, "start"])
            url = rpc_url_wallet(i)
            wait_for_wallet_start(zallet_processes[i], url, i)
            wallets.append(get_rpc_auth_proxy(url, i))
        return wallets

    # ---- faucet / subject accessors ----------------------------------------

    @property
    def faucet(self):
        """The miner wallet (wallet 0) that funds the subjects."""
        return self.wallets[0]

    @property
    def faucet_node(self):
        """The node the faucet mines on (node 0)."""
        return self.nodes[0]

    def subject(self, i=0):
        """Subject wallet `i` (0-based; wallet i+1)."""
        return self.wallets[1 + i]

    def subject_account(self, i=0):
        """Subject `i`'s account UUID."""
        return self.subject(i).z_listaccounts()[0]['account_uuid']

    def _orchard_ua(self, w, acct):
        return w.z_getaddressforaccount(acct, ['orchard'])['address']

    # ---- capability probing ------------------------------------------------

    @staticmethod
    def _is_method_not_found(e):
        msg = str(e.error.get('message', '')).lower()
        return (e.error.get('code') == RPC_METHOD_NOT_FOUND
                or 'method not found' in msg)

    def migration_rpcs_present(self, w):
        """Whether the pool-migration RPC surface is wired in this build. Probes
        `start` with deliberately invalid arguments: an unknown method rejects
        the CALL (method-not-found); a wired method rejects the ARGUMENTS. The
        wallet RPC surface is registered synchronously before the endpoint
        accepts connections, so a single probe is authoritative. (When the run
        uses the zaino backend, whose binary lacks these methods, this correctly
        reports absent and the scenario self-skips; set ZALLET_BACKEND=zebra to
        exercise the migration surface.)"""
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

    # ---- funding: the faucet mints EXACT subject balances ------------------

    def _ensure_faucet_funded(self):
        """Shield the faucet's coinbase once (into the faucet's Sapling pool),
        giving it a large spendable balance to hand out to subjects. Idempotent:
        later subject funding draws on the faucet's remaining balance."""
        if self._faucet_ready:
            return
        node = self.faucet_node
        facct = self.faucet.z_listaccounts()[0]['account_uuid']
        taddr = self.miner_addresses[0]
        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "must still be in the Orchard era before funding")
        faucet_sapling_ua = self.faucet.z_getaddressforaccount(
            facct, ['sapling'])['address']
        _, sapling_zat = shield_coinbase(
            node, self.faucet, taddr, faucet_sapling_ua, facct, Pool.SAPLING)
        assert_true(sapling_zat > 0, "the faucet shielded a spendable balance")
        self._faucet_ua = self.faucet.z_getaddressforaccount(facct)['address']
        self._faucet_acct = facct
        self._faucet_ready = True

    def fund_exact_orchard(self, subject_idx, amounts_zec):
        """Fund subject `subject_idx` with one distinct Orchard note per amount
        in `amounts_zec` (ZEC), EXACTLY: the faucet sends each amount to the
        subject's Orchard receiver, keeping the change itself, so the subject
        holds precisely `sum(amounts_zec)` and nothing more. All sends happen
        pre-NU6.3 so the notes are Orchard (the migration source). Returns the
        subject's exact spendable Orchard balance in zat."""
        self._ensure_faucet_funded()
        node = self.faucet_node
        subject = self.subject(subject_idx)
        sacct = self.subject_account(subject_idx)
        subj_ua = self._orchard_ua(subject, sacct)

        for amount in amounts_zec:
            opid = self.faucet.z_sendmany(
                self._faucet_ua,
                [{'address': subj_ua, 'amount': Decimal(amount)}],
                1, None, PrivacyPolicy.ALLOW_REVEALED_AMOUNTS)
            txid = wait_and_assert_operationid_status(self.faucet, opid)
            assert_true(txid is not None,
                        "faucet -> subject send should succeed")
            node.generate(1)
            self.sync_all()
            wait_for_tx_scanned(self.faucet, txid)
            # Let the faucet's change settle before the next draw.
            wait_account_settled(self.faucet, self._faucet_acct)
            assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                        "the subject's notes must be minted before NU6.3")

        wait_for_account_spendable(subject, sacct, Pool.ORCHARD, min_zat=1)
        wait_account_settled(subject, sacct)
        orchard_zat = account_spendable_zat(subject, sacct, Pool.ORCHARD)
        assert_true(len(orchard_notes(subject)) >= len(amounts_zec),
                    "expected {} distinct Orchard source note(s)".format(
                        len(amounts_zec)))
        assert_true(len(ironwood_notes(subject)) == 0,
                    "no Ironwood notes should exist before migration")
        return orchard_zat

    def fund_exact_note(self, subject_idx, zec):
        """Fund subject `subject_idx` with a single exact Orchard note of `zec`
        ZEC. Returns the subject's exact spendable Orchard balance in zat."""
        return self.fund_exact_orchard(subject_idx, [zec])

    def activate_ironwood(self):
        """Advance the chain past the NU6.3 activation height so the migration is
        allowed (the source notes must already be minted). Mines on the shared
        node and waits for every wallet to reach the tip."""
        node = self.faucet_node
        while node.getblockcount() < IRONWOOD_HEIGHT:
            node.generate(1)
        self.sync_all()

    # ---- lifecycle (per subject wallet) -------------------------------------

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

    def drive_to_completion(self, w, acct, migration_id, on_step=None):
        """Advance one subject's migration one step per call, mining on the shared
        node and letting the wallets catch up between steps, until it completes or
        the step cap is reached. `on_step(step, adv)` runs after each advance.
        Returns True if the migration completed. All wallets share node 0, so the
        subject's just-broadcast transaction is already in the node's mempool when
        it mines; only a post-mine `sync_all` (to let the wallet see the block) is
        needed."""
        node = self.faucet_node
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

    def drive_many_to_completion(self, subjects, on_step=None):
        """Drive SEVERAL subjects' migrations CONCURRENTLY on the one shared node:
        round-robin one advance step per still-running subject, then mine one
        batch that confirms every subject's just-broadcast transaction together
        (they all submit to the same node's mempool), and let the wallets catch
        up. `subjects` is a list of `(wallet, account, migration_id)` tuples.
        `on_step(step, idx, adv)` runs after each subject's advance. Returns a
        list of per-subject completion booleans."""
        node = self.faucet_node
        done = [False] * len(subjects)
        for step in range(self.MAX_ADVANCE_STEPS):
            for idx, (w, acct, mid) in enumerate(subjects):
                if done[idx]:
                    continue
                adv = self.advance(w, acct, mid)
                if on_step is not None:
                    on_step(step, idx, adv)
                if adv['phase'] == 'completed':
                    done[idx] = True
            if all(done):
                return done
            node.generate(self.ADVANCE_MINE_BLOCKS)
            self.sync_all()
            for w, acct, _ in subjects:
                wait_account_settled(w, acct)
        return done

    # ---- external-signer flow ----------------------------------------------

    def start_external(self, w, acct):
        """Start (build) the migration for an EXTERNAL signer: the preparation
        is built UNSIGNED. Returns (start_response, unsigned_prep_transactions),
        where each unsigned transaction is a {'id', 'pczt'} dict."""
        started = getattr(w, START_RPC)(acct, FROM_POOL, TO_POOL, True)
        return started, started['unsigned_transactions']

    def sign_pczt(self, w, acct, pczt):
        """Sign one migration PCZT with the account's spend key. This stands in
        for the hardware/offline device: in a real deployment the unsigned PCZT
        is signed off the wallet host. Returns the signed PCZT (base64)."""
        return getattr(w, SIGN_PCZT_RPC)(acct, pczt)['pczt']

    def apply_signature(self, w, migration_id, tx_id, signed_pczt):
        """Apply a signed PCZT back to its migration transaction (matched by id),
        moving it from awaiting-signature to signed."""
        return getattr(w, APPLY_SIGNATURE_RPC)(migration_id, tx_id, signed_pczt)

    def build_transfers(self, w, acct, migration_id):
        """Build the phase-2 transfers UNSIGNED once the preparation is mined.
        Returns the response; its 'unsigned_transactions' list is empty until the
        whole preparation has mined, and non-empty (awaiting signatures) once the
        transfers are built."""
        return getattr(w, BUILD_TRANSFERS_RPC)(acct, migration_id)

    def sign_and_apply_all(self, w, acct, migration_id, unsigned):
        """Sign each unsigned {'id','pczt'} transaction (device stand-in) and
        apply the signature back, moving each to signed."""
        for u in unsigned:
            signed = self.sign_pczt(w, acct, u['pczt'])
            self.apply_signature(w, migration_id, u['id'], signed)

    def drive_external_to_completion(self, w, acct, migration_id, unsigned_prep,
                                     on_step=None):
        """Drive a migration started for an EXTERNAL signer to completion.

        The wallet builds every transaction UNSIGNED; this test signs each PCZT
        with the account key (standing in for a hardware device) and applies it,
        then `advance` proves and broadcasts it exactly as for the in-process
        path. The preparation PCZTs (passed in as `unsigned_prep`) are signed and
        applied first; then each step, until the transfers are built, it calls
        `build_transfers` (which builds them unsigned only once the preparation
        has mined, so `advance` never signs them in process), signs and applies
        them, and advances. Mines on the shared node between steps. Returns True
        if the migration completed within MAX_ADVANCE_STEPS."""
        node = self.faucet_node
        self.sign_and_apply_all(w, acct, migration_id, unsigned_prep)
        transfers_built = False
        for step in range(self.MAX_ADVANCE_STEPS):
            if not transfers_built:
                bt = self.build_transfers(w, acct, migration_id)
                if bt['unsigned_transactions']:
                    self.sign_and_apply_all(
                        w, acct, migration_id, bt['unsigned_transactions'])
                    transfers_built = True
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

    def ironwood_balance_zat(self, w):
        """The account's total spendable Ironwood balance, in zatoshi, summed
        from its Ironwood notes (each z_listunspent note reports its value in the
        `valueZat` field). A plain data helper: each scenario asserts the concrete
        balance relationship (source, Ironwood, residual, fees, note count)
        INLINE, so the file is self-contained and reviewable."""
        return sum(int(n['valueZat']) for n in ironwood_notes(w))

    def orchard_balance_zat(self, w, acct):
        """The account's spendable Orchard balance in zatoshi (data helper)."""
        return account_spendable_zat(w, acct, Pool.ORCHARD)

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
        # Anchor-bucket ordering, two ways:
        # (1) readiness: no layer-N (>0) transaction is ready while any
        #     layer-(N-1) preparation transaction is unmined;
        # (2) build order: if any layer-N transaction has been BUILT (its state
        #     is past `planned`), every layer-(N-1) transaction must be mined,
        #     because a later layer is signed only once its predecessor mines and
        #     `mined` is terminal. This is the anchor-bucket invariant, checkable
        #     within a single snapshot (no cross-step timing to get wrong).
        prep = [t for t in txs if t['kind'] == 'preparation']
        by_layer = {}
        for t in prep:
            by_layer.setdefault(t['layer'], []).append(t)
        built = ('signed', 'proved', 'broadcast', 'mined')
        for layer in sorted(by_layer):
            if layer == 0:
                continue
            prior_all_mined = all(
                t['state'] == 'mined' for t in by_layer.get(layer - 1, []))
            for t in by_layer[layer]:
                if not prior_all_mined:
                    assert_true(not t['ready'],
                                "layer {} must not be ready while layer {} is "
                                "unmined: {}".format(layer, layer - 1, t))
                    assert_true(t['state'] not in built,
                                "layer {} was built before layer {} fully "
                                "mined: {}".format(layer, layer - 1, t))
        return st
