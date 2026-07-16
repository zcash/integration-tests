#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Orchard -> Ironwood value-pool migration (NU6.3, ZIP 2005), against the Z3
# stack (zebrad + zaino + zallet).
#
# SCAFFOLDING ONLY. The migration itself is still being built: zallet will
# eventually expose the command, and the librustzcash note-split planner crate
# `zcash_ironwood_migration_backend` is still evolving. This module registers
# the end-to-end flow and its fixtures WITHOUT asserting any final behavior, so
# it stays independent of the final zallet/librustzcash API.
#
# Two things keep it from failing the suite while the feature is unimplemented:
#   * It is listed in DISABLED_SCRIPTS in qa/pull-tester/rpc-tests.py, so the
#     runner does not spend a coinbase-maturity window on a test that can only
#     skip today (the framework's equivalent of pytest's skip/xfail: the test is
#     registered in NEW_SCRIPTS but excluded from the run lists). Remove that
#     entry to enable it once the RPC lands.
#   * If it is run directly, it SELF-SKIPS: run_test probes for the migration
#     RPC and, finding none, returns cleanly (exit 0) instead of failing.
#
# The flow, once the feature exists:
#   1. Orchard era (pre-NU6.3): mint a real, spendable v2 Orchard note -- the
#      migration SOURCE. (Mirrors wallet_ironwood_activation.py: a direct
#      many-UTXO Orchard shield can be left unspendable by fee estimation, so we
#      shield into Sapling then pay the Orchard-only receiver.)
#   2. NU6.3 activates, bringing the Ironwood pool live.
#   3. TODO(migration): invoke the migration command / RPC to move the Orchard
#      value into the Ironwood pool via the note-split planner.
#   4. TODO(assert): the Orchard pool drains and the Ironwood pool gains the
#      value, split into planner denominations. The denomination policy
#      ({1,2,5}*10^k ZEC and the sub-0.01 residual) is NOT pinned here because it
#      is still evolving; when finalized, assert only stable, conservation-style
#      invariants (see the TODO block in run_test).
#

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
# mature coinbase) is mined before activation. Mirrors
# wallet_ironwood_activation.py.
IRONWOOD_HEIGHT = 210

# JSON-RPC "method not found" code; an unknown method returns this before it
# parses any argument.
RPC_METHOD_NOT_FOUND = -32601

# A deliberately-invalid argument used only to probe whether a candidate method
# exists. An unknown method rejects the CALL (method-not-found); an existing
# method rejects this ARGUMENT (some other error). Passing it means the probe
# can never trigger a real migration.
PROBE_SENTINEL = '__ironwood_migration_probe__'

# Candidate names for the not-yet-released zallet migration RPC. The final name
# is undecided while the feature evolves, so the probe treats the flow as
# unimplemented (and self-skips) unless one of these resolves to a real method.
# Update this list -- or replace the probe with a proper capability check --
# once zallet settles the RPC surface.
MIGRATION_RPC_CANDIDATES = (
    'z_migrate_pool',
    'z_migratepool',
    'z_migrateironwood',
    'z_migrate_to_ironwood',
)


def orchard_notes(wallet, minconf=1):
    return [u for u in wallet.z_listunspent(minconf)
            if u['pool'] == Pool.ORCHARD]


class WalletIronwoodMigrationTest(IronwoodTestFramework):

    def __init__(self):
        super().__init__()
        # Deferred activation gives an Orchard era in which to mint the v2
        # Orchard note the migration consumes.
        self.activation_heights = nu_activation_ironwood_at(IRONWOOD_HEIGHT)

    def migration_rpc_name(self, w):
        """Return the name of the migration RPC if zallet exposes one, else
        None. Probes each candidate with an invalid sentinel argument and treats
        a 'method not found' error as 'not implemented'."""
        for name in MIGRATION_RPC_CANDIDATES:
            method = getattr(w, name)
            try:
                method(PROBE_SENTINEL)
            except _RPC_EXCEPTIONS as e:
                msg = str(e.error.get('message', '')).lower()
                if (e.error.get('code') == RPC_METHOD_NOT_FOUND
                        or 'method not found' in msg):
                    continue  # this candidate does not exist; try the next
                return name  # exists (rejected the argument, not the method)
            else:
                return name  # exists and (surprisingly) accepted the sentinel
        return None

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        taddr = self.miner_addresses[0]

        # Bring the wallet up to the chain tip so account RPCs are accepted.
        self.sync_all()

        # Capability gate: skip cleanly until zallet exposes the migration RPC.
        # This keeps the module registered and runnable without failing while
        # the feature (and the librustzcash planner) are still evolving.
        rpc_name = self.migration_rpc_name(w)
        if rpc_name is None:
            print("SKIP: no zallet Orchard->Ironwood migration RPC yet "
                  "(tried {}); scaffolding only.".format(
                      ", ".join(MIGRATION_RPC_CANDIDATES)))
            return
        print("Found migration RPC: {}".format(rpc_name))

        # ---- Setup skeleton: mint the migration SOURCE (a v2 Orchard note) --
        acct = w.z_listaccounts()[0]['account_uuid']
        ua = w.z_getaddressforaccount(acct, ['orchard'])['address']
        sapling_ua = w.z_getaddressforaccount(acct, ['sapling'])['address']

        print("Orchard era: mint a spendable Orchard note (pre-NU6.3)...")
        assert_true(node.getblockcount() < IRONWOOD_HEIGHT,
                    "must still be in the Orchard era before funding")
        _, sapling_zat = shield_coinbase(
            node, w, taddr, sapling_ua, acct, Pool.SAPLING)
        orch_target = (Decimal(sapling_zat) / COIN / 2).quantize(Decimal('0.0001'))
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
        print("  Orchard source funded: {} zat".format(orchard_zat))

        # Cross the NU6.3 activation boundary so the Ironwood pool is live.
        if node.getblockcount() < IRONWOOD_HEIGHT:
            node.generate(IRONWOOD_HEIGHT - node.getblockcount())
        assert_true(node.getblockcount() >= IRONWOOD_HEIGHT,
                    "NU6.3 must be active before migrating")
        self.sync_all()

        # ---- TODO(migration): invoke the migration command -----------------
        # The exact argument shape and return value are still being designed in
        # zallet + zcash_ironwood_migration_backend. Wire the real call here,
        # e.g. an opid-returning `w.<rpc_name>(ua)` / `w.<rpc_name>(acct, ...)`,
        # then confirm and scan the resulting transaction(s) as elsewhere:
        #     opid = getattr(w, rpc_name)(...)
        #     mtxid = wait_and_assert_operationid_status(w, opid)
        #     node.generate(1); wait_for_tx_scanned(w, mtxid)
        print("TODO: invoke {} to migrate Orchard -> Ironwood".format(rpc_name))

        # ---- TODO(assert): value moved Orchard -> Ironwood -----------------
        # Do NOT pin the denomination policy here (the {1,2,5}*10^k split and the
        # sub-0.01 residual handling are still evolving). When finalized, assert
        # only stable invariants, for example:
        #   * the Orchard pool drains to (near) zero,
        #   * the Ironwood pool gains the migrated value (minus fees),
        #   * value is conserved across the two pools,
        #   * each produced Ironwood note is a planned denomination.
        print("TODO: assert Orchard drains and Ironwood gains the value "
              "(conservation only; denomination policy left to the feature)")

        print("\nIronwood migration scaffolding reached the migration point.")


if __name__ == '__main__':
    WalletIronwoodMigrationTest().main()
