# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

We do not publish package releases or Git tags for this project. Each version entry in this changelog represents the state of the project at the commit where the changelog update was merged.

## [Unreleased]

### Added

- Coverage for the account-based transaction methods (`z_sendfromaccount`, `z_proposetransaction`, `z_finalizetransaction`): `wallet_z_sendfromaccount.py` covers parameter validation and every fund source end to end, `wallet_z_proposetransaction.py` covers the propose/review/finalize flow, and `wallet_z_send_scenarios.py` drives the workflows the feature exists for (exchange withdrawals and deposit sweeps, an OTC review-then-settle, payroll with memos, a merchant payment, reserve consolidation, a legacy Sapling payout). Four scenarios pin the guarantees rather than the happy path: a fund source isolates its pool rather than falling back to another, an address list restricts to exactly the named addresses, an abandoned proposal moves nothing, and finalize holds the caller to the policy the proposal reported.

- `wallet_transparent_spend.py` covers transparent-to-transparent spending through `z_sendmany`: it shields coinbase and unshields to obtain a non-coinbase transparent UTXO (coinbase cannot be spent transparently), pins both privacy-policy rejections (`AllowRevealedSenders` and `AllowRevealedRecipients` are each insufficient on their own), and asserts the resulting transaction is fully transparent with its change at a fresh internal-scope address.

### Changed

- Rewrote `wallet_z_sendmany.py` for the Z3 stack and re-enabled it. It was disabled because the zcashd original depended on `z_exportviewingkey`, `z_getbalanceforviewingkey`, `sendtoaddress`, and a Sprout section with no Z3 equivalent; it now uses the account/UA API.

- Reactivated `wallet_changeaddresses.py` and `regtest_signrawtransaction.py`, migrated from the `zcashd` RPCs to the Z3 stack. Both were disabled pending an account/UA migration and both depend on transparent spending, which Zallet's `z_sendmany` now supports. Two zcashd behaviours no longer hold and are pinned as such: a transparent source must be named explicitly (`ANY_TADDR` is unsupported), and the change of a transparent-to-shielded send is shielded rather than returned to the transparent pool.

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
