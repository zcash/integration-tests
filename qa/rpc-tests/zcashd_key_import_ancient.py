#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Test that `zallet migrate-zcashd-wallet --no-scan` correctly imports
# keys from a *pre-Sapling, HD-seedless* zcashd wallet.dat into zallet.
#
# Companion to `zcashd_key_import.py`. That test covers a modern wallet
# that spans all network upgrades and carries an HD seed (mnemonic). This
# test covers the opposite extreme: an "ancient" wallet produced by the
# pre-Sapling zcashd binary v1.1.1 that has NO `hdseed` and NO mnemonic -
# only transparent keys generated from system randomness, plus imported
# foreign keys. See the wallet builder's "ancient wallet" mode (`make
# ancient`) [1][2].
#
# The fixture's per-phase `imported_keys` carry `transparent_privkey`
# (`importprivkey`, spendable) and `transparent_watchonly` (`importaddress
# <addr>`, watch-only-by-hash), plus Sprout spending/viewing keys (not
# asserted; zallet does not support Sprout). There is no
# `transparent_pubkey` (`importpubkey`) or `transparent_p2sh` (P2SH
# `importaddress`) entry: zcashd v1.1.1 has no `importpubkey` RPC and its
# `importaddress` takes no P2SH form (see test-ancient-wallet/README.md).
# The per-phase loop below still tolerates them (in case a regenerated
# fixture carries them); the pubkey/P2SH import paths are exercised by
# `zcashd_key_import.py`.
#
# This exercises zallet's seedless import path [3]: with no seed in the
# source wallet, the migration mints a fresh recovery seed and creates a
# legacy transparent account (the "bucket of funds") to hold the imported
# transparent keys. Because that seed is generated at migration time, its
# fingerprint is non-deterministic - so we assert that a derived
# account/seed *exists*, never a specific fingerprint value.
#
# After import, verifies that:
#   * the migration succeeds;
#   * a legacy account with a seed fingerprint now exists
#     (existence only - the fingerprint is non-deterministic);
#   * `listaddresses` contains the imported spendable transparent
#     (`importprivkey`) address(es);
#   * watch-only-by-hash transparent imports (zcashd `importaddress
#     <addr>`) are recorded as SKIPPED - zallet has no plans to support
#     importing watch-only addresses by script-hash alone.
#
# NOTE: this test is expected to fail until the seedless migration stack
# lands: `zewif-zcashd` support for pre-Sapling wallet.dat parsing
# (zewif-zcashd#14) and zallet's seedless legacy-account logic (wallet#384,
# tracked at zallet#739), plus the zallet dependency bump wiring them up.
# See test-ancient-wallet/README.md.
#
# [1] https://github.com/zcash/zcash/tree/master/qa/zcash/wallet-builder (archived)
# [2] https://github.com/zcash/zcash/issues/7196
# [3] https://github.com/zcash/wallet/issues/384
#

import os

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_true,
    indexer_rpc_port,
    node_dir,
    nu_activation_all_at_1_with_ironwood,
    start_wallet,
    update_zallet_conf,
    wallet_dir,
    wallet_rpc_port,
    rpc_port,
    zallet_binary,
    ZalletArgs,
)
from test_framework.zcashd_migration import (
    CheckReporter,
    ImportedKeyKind,
    ancient_test_wallet_path,
    extract_listed_addresses,
    load_manifest,
    load_phase_manifests,
    run_migration,
)


class ZcashdKeyImportAncientTest(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 0   # We set up the wallet manually via migration
        self.num_indexers = 0
        self.cache_behavior = 'clean'
        # The ancient wallet fixture predates NU6.3 but the wallet DB's
        # configured params must be consistent with zebrad's. Activate NU6.3
        # so the migration's regtest activation schedule includes it.
        self.activation_heights = nu_activation_all_at_1_with_ironwood()

    def prepare_chain(self):
        self.nodes[0].generate(1)

    def run_test(self):
        self.wallets = []

        # Locate the ancient (pre-Sapling, seedless) test wallet.
        test_data = ancient_test_wallet_path()
        wallet_dat_path = os.path.join(test_data, 'wallet.dat')
        assert_true(os.path.exists(wallet_dat_path),
            "Ancient test wallet not found at %s" % wallet_dat_path)

        print("Loading manifest from %s" % test_data)
        manifest = load_manifest(test_data)
        phase_manifests = load_phase_manifests(test_data)

        # Set up zallet datadir with config.
        zallet = zallet_binary()
        datadir = wallet_dir(self.options.tmpdir, 0)
        # The zebra backend needs the co-located zebrad's indexer port and
        # state directory; the zaino backend ignores both.
        update_zallet_conf(datadir, rpc_port(0), wallet_rpc_port(0),
                           indexer_port=indexer_rpc_port(0),
                           zebra_state_dir=node_dir(self.options.tmpdir, 0),
                           extra_args=ZalletArgs(activation_heights=self.activation_heights))

        # `run_migration` asserts a zero exit status for both
        # `init-wallet-encryption` and `migrate-zcashd-wallet`, so reaching
        # the next line is itself the "migration succeeds" assertion.
        run_migration(zallet, datadir, wallet_dat_path)

        print("Starting wallet...")
        wallet = start_wallet(0, self.options.tmpdir,
                              zallet_args=ZalletArgs(activation_heights=self.activation_heights))
        self.wallets = [wallet]

        reporter = CheckReporter()

        # === A legacy account/seed now exists ===
        # Migrating a seedless wallet mints a fresh recovery seed and
        # derives a legacy transparent account from it. That account
        # carries a `seedfp` in `z_listaccounts`. The fingerprint is
        # non-deterministic (a new seed each migration), so assert that
        # *some* account exposes a seed fingerprint, not a specific value.
        accounts = wallet.z_listaccounts()
        seed_fps = [acct['seedfp'] for acct in accounts if acct.get('seedfp')]
        print("z_listaccounts returned %d account(s), %d with a seed fingerprint"
              % (len(accounts), len(seed_fps)))
        reporter.check("z_listaccounts exposes a legacy account with a seed fingerprint",
            len(seed_fps) > 0,
            "present (%d)" % len(seed_fps) if seed_fps else "MISSING")

        all_listed = extract_listed_addresses(wallet)
        print("listaddresses contains: %d addresses" % len(all_listed))

        # === Imported transparent keys ===
        # An ancient wallet has no HD-derived addresses; everything of
        # interest arrives through the per-phase `imported_keys` records.
        # For this pre-Sapling (v1.1.1) fixture only the first and last
        # apply; the pubkey/P2SH kinds are listed for robustness but are
        # absent (see module docstring):
        #   * transparent_privkey   -> importprivkey   (spendable)
        #   * transparent_pubkey    -> importpubkey    (watch-only pubkey)
        #   * transparent_p2sh      -> importaddress <redeemScript>
        #   * transparent_watchonly -> importaddress <addr>  (by hash; SKIPPED)
        print("Verifying imported keys per phase...")
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
                    reporter.skip("listaddresses phase %d %s: %s" % (
                             phase, kind.replace('_', ' '), addr),
                         "SKIPPED - watch-only-by-hash not supported by zallet")
                    continue
                reporter.check("listaddresses phase %d %s: %s" % (
                          phase, kind.replace('_', ' '), addr),
                      addr in all_listed,
                      "present" if addr in all_listed else "MISSING")

        # === The wallet's own transparent addresses ===
        # Whatever bare transparent addresses the manifest records (e.g. the
        # coinbase recipient) should also be surfaced by listaddresses.
        for addr in manifest['all_addresses']['transparent']:
            reporter.check("listaddresses transparent: %s" % addr,
                addr in all_listed,
                "present" if addr in all_listed else "MISSING")

        reporter.finish()


if __name__ == '__main__':
    ZcashdKeyImportAncientTest().main()
