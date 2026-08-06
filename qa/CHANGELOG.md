# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

We do not publish package releases or Git tags for this project. Each version entry in this changelog represents the state of the project at the commit where the changelog update was merged.

## [Unreleased]

### Added

- `wallet_transparent_spend.py` covers transparent-to-transparent spending through `z_sendmany`: it shields coinbase and unshields to obtain a non-coinbase transparent UTXO (coinbase cannot be spent transparently), pins both privacy-policy rejections (`AllowRevealedSenders` and `AllowRevealedRecipients` are each insufficient on their own), and asserts the resulting transaction is fully transparent with its change at a fresh internal-scope address.

- `wallet_legacy_pool_spend.py` covers `z_sendmany`'s `ANY_TADDR` source, which spends the legacy `zcashd` pool of funds. Zallet holds that pool in the account derived from the migrated wallet's mnemonic at ZIP 32 index `0x7FFFFFFF`, so the test creates that account with `z_recoveraccounts` and names its seed to the wallet through the new `ZalletArgs.legacy_pool_seed_fingerprint` (which sets `features.legacy_pool_seed_fingerprint` in `zallet.toml`). It pins both configuration rejections (the option unset, and set to a seed with no legacy account), funds the pool across two transparent receivers such that neither covers the payment alone, pins the `AllowLinkingAccountAddresses` requirement that the resulting linkage carries (and that a transparent recipient on top of it needs `NoPrivacy`), and asserts the spend draws on both addresses.

### Removed

- Retired everything that built, ran, or was shaped around a live `zcashd`, now that it has reached end of life and been archived. `qa/zcash/smoke_tests.py` and `qa/zcash/create_benchmark_archive.py` drove a real `zcashd`/`zcash-cli` and had no caller; `maxblocksinflight.py`, `maxuploadtarget.py` and `p2p-acceptblock.py` imported a `ZCASHD_BINARY` symbol that no longer exists and could not even be imported.

- Retired the 109-entry `DISABLED_SCRIPTS` backlog: 104 tests parked when the framework was retargeted from `zcashd` to the Z3 stack, none of which had run in CI for over a year. What they covered is recorded in `doc/book/src/dev/migration-backlog.md`, including the 24 that name a still-missing Zallet or Zebra RPC. Five tests stay disabled-but-present because they target the Z3 stack and are blocked on a specific tracked bug: `addnode.py`, `wallet_ironwood_reorg.py`, `wallet_ironwood_birthday.py`, `zcashd_key_import.py` and `zcashd_key_import_db.py`.

  With them go the framework modules nothing imports any more (the Bitcoin Core P2P harness `p2p`, `comptool`, `blockstore`, `blocktools`, `key`, `netutil`, `socks5`, plus `flyclient` and `zip317`), `ComparisonTestFramework`, the zcashd-format chain and wallet fixtures (`cache/sprout`, `cache/golden-v5.6.0`, `cache/tarnished-v5.6.0`, `golden/*.tar.gz`) and the `init_persistent` / `persist_node_caches` / `check_node_log` machinery that read and wrote the zcashd `node{i}/regtest/` datadir layout. The migration path *off* zcashd is untouched: `zallet migrate-zcashd-wallet`, the `test-wallet/` `wallet.dat` fixture, the `zcashd-import` build feature, the legacy-pool semantics, and the `zcash.conf` written for lightwalletd all remain.

- `EXTENDED_SCRIPTS` and `ZMQ_SCRIPTS`, with `--extended`, `--new-only` and `--nozmq`. `EXTENDED_SCRIPTS` was concatenated into `ALL_SCRIPTS` without being filtered against `DISABLED_SCRIPTS` and was never run by any workflow; `ZMQ_SCRIPTS`'s only entry was disabled. `NEW_SCRIPTS` merges back into `BASE_SCRIPTS`, and `pyzmq` and `base58` drop out of the Python dependencies with the tests that used them.

- `qa/pull-tester/tests_config.ini` and its autoconf template. The `@abs_top_srcdir@` / `@BUILD_BITCOIND_TRUE@` substitutions were never expanded because this repository has no `configure`, so the file only ever supplied constants. `rpc-tests.py` now derives the repository root from its own location — which also makes it work from any working directory — and the `ENABLE_WALLET`/`ENABLE_UTILS`/`ENABLE_BITCOIND` gate is gone.

### Changed

- Renamed the inherited `bitcoind` vocabulary to what it has actually meant since the migration: `bitcoind_processes` is now `zebrad_processes` and `wait_bitcoinds` is now `wait_zebrads`. The `--nocleanup`, `--noshutdown` and `--srcdir` help strings no longer describe `zcashd`/`bitcoind` binaries.

- Reactivated `wallet_changeaddresses.py` and `regtest_signrawtransaction.py`, migrated from the `zcashd` RPCs to the Z3 stack. Both were disabled pending an account/UA migration and both depend on transparent spending, which Zallet's `z_sendmany` now supports. Two zcashd behaviours no longer hold and are pinned as such: a send from an ordinary account must name its transparent source explicitly (`ANY_TADDR` selects the legacy `zcashd` pool specifically, not any transparent address in the wallet), and the change of a transparent-to-shielded send is shielded rather than returned to the transparent pool.

- Support Zallet's launcher-plus-backend structure: `zallet` is a launcher that execs a per-backend binary. CI builds the launcher plus the `zallet-zaino` backend binary and ships both to the test runners, and the default `zallet.toml` selects the Zaino backend via its top-level `backend` key. The build falls back to the single-binary layout when the checked-out wallet has no `backends/` directory.

### Fixed

- CI now schedules every enabled test. The shard count was split between "old" and "new" tests in proportion to their sizes, so once `converttex.py` was the only enabled entry left in `BASE_SCRIPTS` the ratio reached `10 // 28 == 0`; the `if old_shards > 0` guard then skipped the loop that assigns those tests, and `converttex.py` was scheduled onto no shard at all. Tests are now assigned round-robin across all shards.

- `rebuild_cache` no longer fails when the 8-node zebrad mesh does not converge over P2P within `sync_blocks_with_reconnect`'s retries (zebra #10329, #10332): the straggler recovery that already backstopped the post-restart reconnect — copying the missing blocks directly via `getblock`/`submitblock` — is hoisted into a shared `copy_missing_blocks` helper and now also backstops the per-round sync in the mining loop. Cache creation therefore no longer depends on P2P block relay for correctness, only for speed.

## [0.1.0] - 2025-11-11

In this first (pseudo)-release, we document all the changes made while migrating from the original Zcash test framework (`zcashd` only) to the new Z3 stack (`zebrad` + `zallet`), now hosted within the Zebra repository as part of the zebra-rpc crate.

As a reference, we cloned the Zcash repository at the `v6.10.0` tag and replaced its entire `qa` folder with the one developed for Zebra.
The resulting diff can be viewed here: https://github.com/zcash/zcash/compare/v6.10.0...oxarbitrage:zcash:zebra-qa?expand=1

Most of the legacy tests were removed. Since all tests require substantial modification to work with the new stack, we decided to migrate the framework first and then port tests individually as needed.

Below is a summary of the major changes:

### Deleted

- All existing tests were removed.
- `util.py`: removed Zcash-specific functionality.
- Compressed data folders (`cache`, `golden`) were deleted.
- The zcash folder (which contained smoke tests, dependency tests, and the full test suite) was removed.

### Changed

- Adapted existing `zcashd` utilities for use with `zebrad`.
- Disabled cache functionality (the supporting code remains available for future re-enablement).
- Updated argument handling for the Zebra node (different from `zcashd`).
- The following tests were modified to run under the new framework:
    - `wallet.py`
    - `nuparams.py`
    - `getmininginfo.py`
    - `feature_nu6_1.py`
    - `create_cache.py`

### Added

- New test cases:
    - `addnode.py`
    - `getrawtransaction_sidechain.py`
    - `fix_block_commitments.py`
    - `feature_nu6.py`
    - `feature_backup_non_finalized_state.py`
- Introduced a non-authenticated proxy (`proxy.py`), cloned from `authproxy.py` but with authentication removed. 
- Integrated the `zallet` process into the framework. Since the new stack requires both node and wallet processes, utilities for starting, configuring, and managing `zallet` were added following the same pattern as the node helpers.
- Added a `toml` dependency for manipulating Zebra and Zallet configuration files.
- Introduced base configuration templates for both Zebra and Zallet. The framework updates these at startup (setting ports and other runtime data), while tests can override parameters as needed.
- Added a configuration module to simplify passing runtime arguments from tests to the Zebra node.
