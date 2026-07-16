# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

We do not publish package releases or Git tags for this project. Each version entry in this changelog represents the state of the project at the commit where the changelog update was merged.

## [Unreleased]

### Added

- `wallet_ironwood_migration.py` adds scaffolding for the Orchard -> Ironwood value-pool migration (NU6.3, ZIP 2005). It stands up the fixtures (deferred NU6.3 so a real v2 Orchard note can be minted as the migration source) and marks TODO points where the still-evolving zallet migration RPC and the `zcash_ironwood_migration_backend` note-split planner will plug in, without asserting any final behavior (the `{1,2,5}*10^k` denomination policy is deliberately not pinned). It self-skips when no migration RPC is exposed, and is listed in `DISABLED_SCRIPTS` so the suite does not run it until the feature lands.

- `wallet_transparent_spend.py` covers transparent-to-transparent spending through `z_sendmany`: it shields coinbase and unshields to obtain a non-coinbase transparent UTXO (coinbase cannot be spent transparently), pins both privacy-policy rejections (`AllowRevealedSenders` and `AllowRevealedRecipients` are each insufficient on their own), and asserts the resulting transaction is fully transparent with its change at a fresh internal-scope address.

- `wallet_legacy_pool_spend.py` covers `z_sendmany`'s `ANY_TADDR` source, which spends the legacy `zcashd` pool of funds. Zallet holds that pool in the account derived from the migrated wallet's mnemonic at ZIP 32 index `0x7FFFFFFF`, so the test creates that account with `z_recoveraccounts` and names its seed to the wallet through the new `ZalletArgs.legacy_pool_seed_fingerprint` (which sets `features.legacy_pool_seed_fingerprint` in `zallet.toml`). It pins both configuration rejections (the option unset, and set to a seed with no legacy account), funds the pool across two transparent receivers such that neither covers the payment alone, pins the `AllowLinkingAccountAddresses` requirement that the resulting linkage carries (and that a transparent recipient on top of it needs `NoPrivacy`), and asserts the spend draws on both addresses.

### Changed

- Reactivated `wallet_changeaddresses.py` and `regtest_signrawtransaction.py`, migrated from the `zcashd` RPCs to the Z3 stack. Both were disabled pending an account/UA migration and both depend on transparent spending, which Zallet's `z_sendmany` now supports. Two zcashd behaviours no longer hold and are pinned as such: a send from an ordinary account must name its transparent source explicitly (`ANY_TADDR` selects the legacy `zcashd` pool specifically, not any transparent address in the wallet), and the change of a transparent-to-shielded send is shielded rather than returned to the transparent pool.

- Support Zallet's launcher-plus-backend structure: `zallet` is a launcher that execs a per-backend binary. CI builds the launcher plus the `zallet-zaino` backend binary and ships both to the test runners, and the default `zallet.toml` selects the Zaino backend via its top-level `backend` key. The build falls back to the single-binary layout when the checked-out wallet has no `backends/` directory.

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
