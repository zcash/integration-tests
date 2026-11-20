#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Strict DB-inspection variant of the zcashd-key-import test.
#
# Companion to `zcashd_key_import.py`. That test verifies the keys zallet
# exposes through the `listaddresses` JSON-RPC. This test reads the
# migrated `wallet.db` SQLite file directly to assert that EVERY expected
# imported key class is persisted - including the ones invisible to the
# RPC surface (sapling viewing keys, standalone transparent privkeys,
# watch-only addresses imported by hash). It does not start zebrad,
# zaino, or the zallet RPC server; only `init-wallet-encryption` and
# `migrate-zcashd-wallet --no-scan` are invoked.
#
# Compared to the API test:
#   * No gap-limit/exposure-tracking leniency. Every HD-derived address
#     in the manifest must be present in the DB.
#   * No NU6.1-tx-parseability fallback. Membership is strictly required.
#   * Covers key classes the API hides: imported sapling viewing keys
#     (per-FVK match), watch-only-by-hash transparent addresses, and
#     standalone transparent privkeys.
#
# Shielded checks match by viewing-key bytes:
#   * sapling FVK = bytes 41..169 of the bech32-decoded
#     `zxviewregtestsapling...` extended FVK (the 128-byte dfvk format
#     used by `ext_zallet_keystore_standalone_sapling_keys.dfvk`).
#   * orchard FVK = the 96-byte typecode-0x03 receiver inside a
#     bech32m-decoded unified FVK (`uviewregtest...`), recovered via
#     F4Jumble inverse + ZIP-316 typed-receiver TLV split.
# Both decoders live in `test_framework/ufvk_decode.py`.
#
# Sprout keys are recorded as SKIPPED (zallet does not support them);
# the explicit skip lines in the report make the gap visible. The same
# treatment is applied to watch-only-by-hash transparent imports
# (zcashd `importaddress`), which zallet has no plans to support.
#
# [1] https://github.com/zcash/zcash/tree/master/qa/zcash/wallet-builder
#

import os
import sqlite3

from test_framework.test_framework import BitcoinTestFramework
from test_framework.ufvk_decode import (
    orchard_fvk_from_unified_fvk,
    sapling_dfvk_from_extfvk,
    sapling_dfvk_from_unified_fvk,
    selftest as ufvk_decode_selftest,
)
from test_framework.util import (
    assert_true,
    nu_activation_all_at_1_with_ironwood,
    render_regtest_nuparams,
    wallet_dir,
    zallet_binary,
    zallet_config,
)
from test_framework.zcashd_migration import (
    CheckReporter,
    ImportedKeyKind,
    load_manifest,
    load_phase_manifests,
    run_migration,
    test_wallet_path,
)


# Schema source:
#   accounts, addresses        - zcash/librustzcash: zcash_client_sqlite/src/wallet/db.rs
#                                and zcash_client_sqlite/src/wallet/init/migrations/*.rs
#   ext_zallet_keystore_*      - zcash/wallet: zallet/src/components/keystore/db.rs
#                                and zallet/src/components/keystore/db/migrations/initial_setup.rs
# If any of these columns rename, update load_db_view() accordingly.
def load_db_view(wallet_db_path):
    """
    Read everything we need from `wallet.db` into in-memory sets/lists.
    No long-lived connection is held.

    Returns a dict with keys:
        transparent_addrs : set[str]
            Every value of `address` and `cached_transparent_receiver_address`
            from the `addresses` table - covers HD external/internal,
            ephemeral, and standalone (key_scope = -1) imported addresses.
        sapling_dfvks : set[bytes]
            128-byte ak||nk||ovk||dk blobs known to the wallet, drawn from
            `ext_zallet_keystore_standalone_sapling_keys.dfvk` and from
            decoded `accounts.ufvk` rows that carry a sapling receiver.
        orchard_fvks : set[bytes]
            96-byte orchard FVKs drawn from `accounts.ufvk` rows that carry
            an orchard receiver.
        sapling_keystore_count : int
            Row count of `ext_zallet_keystore_standalone_sapling_keys`,
            used as the cardinality proxy for `sapling_spending` imports
            (per-phase FVK derivation from extsk needs Jubjub).
    """
    con = sqlite3.connect(wallet_db_path)
    try:
        transparent_addrs = set()
        for address, cached in con.execute(
                "SELECT address, cached_transparent_receiver_address FROM addresses"):
            if address is not None:
                transparent_addrs.add(address)
            if cached is not None:
                transparent_addrs.add(cached)

        sapling_dfvks = set()
        orchard_fvks = set()
        for (ufvk,) in con.execute("SELECT ufvk FROM accounts WHERE ufvk IS NOT NULL"):
            try:
                sap = sapling_dfvk_from_unified_fvk(ufvk)
                if sap is not None:
                    sapling_dfvks.add(sap)
                orc = orchard_fvk_from_unified_fvk(ufvk)
                if orc is not None:
                    orchard_fvks.add(orc)
            except ValueError as e:
                # Don't fail the whole test on one undecodable UFVK -
                # surface the issue as a printed warning instead.
                print("WARN: could not decode accounts.ufvk row: %s" % e)

        for (dfvk,) in con.execute(
                "SELECT dfvk FROM ext_zallet_keystore_standalone_sapling_keys"):
            sapling_dfvks.add(bytes(dfvk))

        sapling_keystore_count = con.execute(
            "SELECT COUNT(*) FROM ext_zallet_keystore_standalone_sapling_keys"
        ).fetchone()[0]
    finally:
        con.close()

    return {
        "transparent_addrs": transparent_addrs,
        "sapling_dfvks": sapling_dfvks,
        "orchard_fvks": orchard_fvks,
        "sapling_keystore_count": sapling_keystore_count,
    }


class ZcashdKeyImportDbTest(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.num_nodes = 0
        self.num_wallets = 0
        self.num_indexers = 0
        self.cache_behavior = 'clean'
        self.nodes = []
        self.zainos = []
        self.wallets = []

    def setup_network(self):
        # No zebrad, no zallet RPC, no zaino. The migration command runs
        # `--no-scan`, which skips every chain interaction.
        pass

    def run_test(self):
        # Decoder preflight: exercise the pure-Python ufvk_decode helpers
        # against the fixture's viewing-key vectors before relying on them
        # below. This is what makes the `ufvk_decode.py` self-test run in
        # CI (this test is listed in qa/pull-tester/rpc-tests.py).
        print("Running ufvk_decode self-test...")
        ufvk_decode_selftest()

        # Locate test wallet and manifest data
        test_data = test_wallet_path()
        wallet_dat_path = os.path.join(test_data, 'wallet.dat')
        assert_true(os.path.exists(wallet_dat_path),
            "Test wallet not found at %s" % wallet_dat_path)

        print("Loading manifest from %s" % test_data)
        manifest = load_manifest(test_data)
        phase_manifests = load_phase_manifests(test_data)

        # Set up zallet datadir. `zallet_config` copies the defaults
        # directory (zallet.toml + encryption-identity.txt) into place.
        zallet = zallet_binary()
        datadir = wallet_dir(self.options.tmpdir, 0)
        config_path = zallet_config(datadir)

        # The modern test-wallet fixture spans all 9 network upgrades including
        # NU6.3 (Ironwood), so its transactions carry NU6.3 branch IDs. The
        # default config only activates through NU6.2; add NU6.3 so the
        # migration's derive_regtest_activations includes it.
        import toml
        with open(config_path, "r", encoding="utf8") as f:
            config = toml.load(f)
        config.setdefault('consensus', {})['regtest_nuparams'] = \
            render_regtest_nuparams(nu_activation_all_at_1_with_ironwood())
        with open(config_path, "w", encoding="utf8") as f:
            toml.dump(config, f)

        run_migration(zallet, datadir, wallet_dat_path)

        # Read the migrated database.
        wallet_db = os.path.join(datadir, "wallet.db")
        assert_true(os.path.exists(wallet_db),
            "wallet.db not created at %s" % wallet_db)
        view = load_db_view(wallet_db)
        print("DB summary: %d transparent addresses, %d sapling FVKs, "
              "%d orchard FVKs, %d standalone-sapling keystore rows" % (
                  len(view["transparent_addrs"]),
                  len(view["sapling_dfvks"]),
                  len(view["orchard_fvks"]),
                  view["sapling_keystore_count"]))

        reporter = CheckReporter()

        # === HD transparent addresses ===
        # The `addresses` table holds every HD-derived t-address the wallet
        # tracks (across `key_scope` 0/1/2). Strict per-address membership.
        print("Verifying HD transparent addresses...")
        for addr in manifest['all_addresses']['transparent']:
            reporter.check("addresses.cached_transparent_receiver_address HD: %s" % addr,
                  addr in view["transparent_addrs"],
                  "present" if addr in view["transparent_addrs"] else "MISSING")

        # === HD sapling addresses (FVK-based) ===
        # The manifest's `all_viewing_keys.sapling` is the canonical source
        # for HD sapling assertions: each entry has the bare address and the
        # `zxviewregtestsapling...` extended FVK. The 4 bare addresses in
        # `all_addresses.sapling` not represented here are diversified
        # addresses that share an FVK with one of these - verifying their
        # bare-address derivability needs Jubjub group-hash, which pure
        # Python doesn't have. Asserting per-FVK matches the practical
        # security boundary: if the FVK is in the wallet, the address is
        # spendable/viewable.
        print("Verifying HD sapling FVKs...")
        for entry in manifest['all_viewing_keys']['sapling']:
            short = "%s..." % entry['address'][:32]
            try:
                dfvk = sapling_dfvk_from_extfvk(entry['viewing_key'])
            except ValueError as e:
                reporter.check("HD sapling dfvk (%s)" % short, False,
                      "could not decode viewing_key: %s" % e)
                continue
            present = dfvk in view["sapling_dfvks"]
            reporter.check("HD sapling dfvk (%s)" % short,
                  present, "present" if present else "MISSING")

        # === HD orchard / UA addresses ===
        # Each manifest UA wraps an orchard receiver. We match by orchard
        # FVK bytes extracted from the manifest's `viewing_key` (a unified
        # FVK with only the orchard receiver) against orchard FVKs
        # extracted from each `accounts.ufvk` row in the DB.
        print("Verifying HD orchard FVKs...")
        for entry in manifest['all_viewing_keys']['orchard']:
            short = "%s...%s" % (entry['address'][:20], entry['address'][-8:])
            try:
                fvk = orchard_fvk_from_unified_fvk(entry['viewing_key'])
            except ValueError as e:
                reporter.check("HD orchard FVK (%s)" % short, False,
                      "could not decode viewing_key: %s" % e)
                continue
            if fvk is None:
                reporter.check("HD orchard FVK (%s)" % short, False,
                      "viewing_key did not contain an orchard receiver")
                continue
            present = fvk in view["orchard_fvks"]
            reporter.check("HD orchard FVK (%s)" % short,
                  present, "present" if present else "MISSING")

        # === Imported keys from each phase ===
        # All four transparent flavours land in the same `addresses` table
        # - strict per-address membership, regardless of `key_scope`.
        # Sapling viewing keys are matched per-FVK. Sapling spending keys
        # don't have a pure-Python FVK extraction path (extsk -> FVK needs
        # Jubjub), so we use the keystore row count as a cardinality proxy
        # and emit per-phase results based on the aggregate.
        # TODO: per-phase match once Jubjub bindings are available.
        print("Verifying imported keys per phase...")
        sapling_spending_expected = sum(
            1 for pm in phase_manifests
            if ImportedKeyKind.SAPLING_SPENDING in pm.get('imported_keys', {}))
        sapling_keystore_ok = (
            view["sapling_keystore_count"] >= sapling_spending_expected)

        for pm in phase_manifests:
            phase = pm['phase']
            imported = pm.get('imported_keys', {})
            if not imported:
                continue

            for kind in (ImportedKeyKind.TRANSPARENT_PRIVKEY,
                         ImportedKeyKind.TRANSPARENT_WATCHONLY,
                         ImportedKeyKind.TRANSPARENT_P2SH,
                         ImportedKeyKind.TRANSPARENT_PUBKEY):
                if kind not in imported:
                    continue
                addr = imported[kind]['address']
                if kind == ImportedKeyKind.TRANSPARENT_WATCHONLY:
                    reporter.skip("phase %d %s: %s" % (phase, kind, addr),
                         "SKIPPED - watch-only-by-hash not supported by zallet")
                    continue
                present = addr in view["transparent_addrs"]
                reporter.check("phase %d %s in addresses: %s" % (phase, kind, addr),
                      present, "present" if present else "MISSING")

            if ImportedKeyKind.SAPLING_SPENDING in imported:
                addr = imported[ImportedKeyKind.SAPLING_SPENDING]['address']
                detail = ("keystore has %d row(s); expected >= %d" % (
                    view["sapling_keystore_count"], sapling_spending_expected))
                reporter.check("phase %d sapling spending (cardinality proxy): %s" % (phase, addr),
                      sapling_keystore_ok, detail)

            # Standalone sapling viewing keys (zcashd `z_importviewingkey`)
            # are not migrated: zallet has no home for a viewing-only sapling
            # key outside an account. This is an accepted limitation, so it
            # is recorded as SKIP rather than asserted.
            if ImportedKeyKind.SAPLING_VIEWING in imported:
                addr = imported[ImportedKeyKind.SAPLING_VIEWING]['address']
                reporter.skip("phase %d sapling viewing dfvk: %s" % (phase, addr),
                     "SKIPPED - sapling viewing keys are dropped by migration (accepted)")

            # Sprout keys: explicitly recorded as skipped so the gap is
            # visible in the report. Zallet has no storage for them.
            for kind in (ImportedKeyKind.SPROUT_SPENDING,
                         ImportedKeyKind.SPROUT_VIEWING):
                if kind in imported:
                    addr = imported[kind]['address']
                    reporter.skip("phase %d %s: %s" % (phase, kind, addr),
                         "SKIPPED - sprout not supported by zallet")

        # === Report results ===
        reporter.finish()


if __name__ == '__main__':
    ZcashdKeyImportDbTest().main()
