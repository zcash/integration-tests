#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

"""
Shared helpers for the `zcashd_key_import*.py` tests: fixture path
resolution, manifest loading, the `init-wallet-encryption` +
`migrate-zcashd-wallet --no-scan` invocation sequence, and the
per-check pass/fail/skip reporter.
"""

import json
import os
import subprocess

from enum import Enum

from test_framework.util import assert_equal


_MIGRATION_TIMEOUT_SECS = 120


class ImportedKeyKind(str, Enum):
    """
    Keys of the `imported_keys` object in a phase manifest. `str`-backed so
    members compare equal to (and hash like) the raw manifest strings, and can
    index the manifest dict directly. (Plain `str, Enum` rather than 3.11's
    `StrEnum` so the framework runs on the CI interpreter, Python 3.10.)
    """
    TRANSPARENT_PRIVKEY = 'transparent_privkey'
    TRANSPARENT_WATCHONLY = 'transparent_watchonly'
    TRANSPARENT_P2SH = 'transparent_p2sh'
    TRANSPARENT_PUBKEY = 'transparent_pubkey'
    SAPLING_SPENDING = 'sapling_spending'
    SAPLING_VIEWING = 'sapling_viewing'
    SPROUT_SPENDING = 'sprout_spending'
    SPROUT_VIEWING = 'sprout_viewing'


def test_wallet_path():
    """Resolve the path to the test wallet data directory."""
    return os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        '..', 'test-wallet')


def ancient_test_wallet_path():
    """
    Resolve the path to the "ancient" test wallet data directory.

    This fixture is a pre-Sapling, HD-seedless zcashd `wallet.dat` (a
    transparent-only wallet with no `hdseed`/mnemonic) produced by the
    regtest wallet builder. It exercises zallet's seedless import path,
    where the migration mints a fresh recovery seed to back the legacy
    transparent account. See zcash/zcash#7196 for the builder support and
    zcash/integration-tests#135 for the test itself.
    """
    return os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        '..', 'test-ancient-wallet')


def load_manifest(output_path):
    manifest_path = os.path.join(output_path, 'full_manifest.json')
    with open(manifest_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_phase_manifests(output_path):
    manifests_dir = os.path.join(output_path, 'manifests')
    phase_manifests = []
    for filename in sorted(os.listdir(manifests_dir)):
        if filename.startswith('phase_') and filename.endswith('.json'):
            with open(os.path.join(manifests_dir, filename), 'r', encoding='utf-8') as f:
                phase_manifests.append(json.load(f))
    return phase_manifests


def run_migration(zallet_binary, datadir, wallet_dat_path):
    """
    Run `init-wallet-encryption` followed by `migrate-zcashd-wallet`
    against `wallet_dat_path`.

    The migration is invoked with `--no-scan`, which skips all chain
    interaction (Zebra cannot serve zcashd regtest chain data), so no
    zebrad is required. `--buffer-wallet-transactions` is deliberately
    not passed: the buffered path currently errors when all transactions
    are unmined (separate upstream bug, tracked elsewhere). The migration
    shells out to `db_dump` (a Berkeley DB utility), which must be on
    PATH.

    Raises AssertionError on non-zero return or timeout. Prints
    stdout/stderr of the migration on failure.
    """
    print("Initializing wallet encryption...")
    try:
        result = subprocess.run(
            [zallet_binary, "-d=" + datadir, "init-wallet-encryption"],
            capture_output=True, text=True, timeout=_MIGRATION_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "init-wallet-encryption timed out after %ds" % _MIGRATION_TIMEOUT_SECS)
    if result.returncode != 0:
        print("STDOUT: %s" % result.stdout)
        print("STDERR: %s" % result.stderr)
    assert_equal(result.returncode, 0,
        "init-wallet-encryption failed: %s" % result.stderr)

    print("Importing keys from zcashd wallet...")
    try:
        result = subprocess.run([
            zallet_binary, "-d=" + datadir, "migrate-zcashd-wallet",
            "--path", wallet_dat_path,
            "--no-scan",
            "--allow-warnings",
            "--this-is-beta-code-and-you-will-need-to-redo-the-migration-later",
        ], capture_output=True, text=True, timeout=_MIGRATION_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "migrate-zcashd-wallet timed out after %ds" % _MIGRATION_TIMEOUT_SECS)
    if result.returncode != 0:
        print("STDOUT: %s" % result.stdout)
        print("STDERR: %s" % result.stderr)
    assert_equal(result.returncode, 0,
        "migrate-zcashd-wallet failed: %s" % result.stderr)


def extract_listed_addresses(wallet):
    """
    Collect the set of all address strings from the listaddresses RPC.

    Handles the source-grouped format returned by zallet:
      [{"source": "mnemonic_seed",
        "transparent": {"addresses": [...], "changeAddresses": [...]},
        "derived_transparent": [{"addresses": [...], "changeAddresses": [...]}],
        "sapling": [{"addresses": [...]}],
        "unified": [{"addresses": [{"address": ...}]}]}, ...]

    For unified addresses, also decomposes each UA via z_listunifiedreceivers
    and adds the individual typed receivers (p2pkh, sapling, orchard) to the
    result set. This allows matching against bare sapling/transparent addresses
    in the manifest, since zallet wraps legacy sapling keys in sapling-only UAs.
    """
    result = set()
    for group in wallet.listaddresses():
        # Non-derived transparent addresses and change addresses
        t_section = group.get('transparent', {})
        if isinstance(t_section, dict):
            for addr in t_section.get('addresses', []):
                result.add(addr)
            for addr in t_section.get('changeAddresses', []):
                result.add(addr)

        # Derived transparent addresses (BIP 44)
        for dt_group in group.get('derived_transparent', []):
            for addr in dt_group.get('addresses', []):
                result.add(addr)
            for addr in dt_group.get('changeAddresses', []):
                result.add(addr)

        # Sapling addresses
        for sapling_group in group.get('sapling', []):
            for addr in sapling_group.get('addresses', []):
                result.add(addr)

        # Unified addresses - add the UA itself, plus decomposed receivers
        for ua_group in group.get('unified', []):
            for addr_entry in ua_group.get('addresses', []):
                ua_addr = addr_entry.get('address', '')
                if ua_addr:
                    result.add(ua_addr)
                    receivers = wallet.z_listunifiedreceivers(ua_addr)
                    for receiver_addr in receivers.values():
                        result.add(receiver_addr)

    return result


class CheckReporter:
    """
    Per-check pass/fail/skip reporter. Each `check()`/`skip()` prints its
    result inline so that a crash mid-test still leaves a record of which
    checks ran; `finish()` emits the summary, re-lists any failures, and
    raises AssertionError if any check failed.
    """

    def __init__(self):
        self._checks = []   # list[(label, passed, detail)]
        self._skipped = []  # list[(label, detail)]

    def check(self, label, passed, detail=""):
        self._checks.append((label, passed, detail))
        print("  %s %s: %s" % ("PASS" if passed else "FAIL", label, detail))

    def skip(self, label, detail):
        self._skipped.append((label, detail))
        print("  SKIP %s: %s" % (label, detail))

    def finish(self):
        passed = [c for c in self._checks if c[1]]
        failed = [c for c in self._checks if not c[1]]

        print("")
        if self._skipped:
            print("%d/%d checks passed (%d skipped)" % (
                len(passed), len(self._checks), len(self._skipped)))
        else:
            print("%d/%d checks passed" % (len(passed), len(self._checks)))
        if failed:
            print("")
            print("FAILED checks:")
            for label, _, detail in failed:
                print("  - %s: %s" % (label, detail))
            raise AssertionError(
                "%d key import checks failed (see above)" % len(failed))
