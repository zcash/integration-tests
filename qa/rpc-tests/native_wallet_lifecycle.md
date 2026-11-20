# Native Zinder wallet lifecycle

This opt-in Regtest scenario exercises the public Zallet launcher and native
WalletQuery backend against released Zinder v0.21.0. Run it only on a dedicated,
disposable chain where mining is authorized. The native endpoint must retain
full blocks and match this schedule: Overwinter through Canopy at 1, NU5 at 2,
NU6 at 3, NU6.1 at 4, NU6.2 at 5, NU6.3 at 6, and NU7 at 140.

Build Zallet's launcher with its pinned toolchain and the independent
`backends/zinder` workspace with Rust 1.100. Ordinary builds need no optional
features. `--backend-binary` can select a separately built binary.

The scenario funds a public test mnemonic, restores it into an independent
empty database, and checks the recovered full viewing key and known owned
Ironwood transaction. A separate native producer submits an incoming private
payment. The restored wallet must observe it before mining, restart while it
is pending, and actually scan the native mempool snapshot again. It then checks
canonical mining, scan-back, persisted balance and transaction state after
another process restart, and a fresh block after resuming. Transaction
fingerprints link the independent observations. Endpoint attribution is
configured-only, with snapshots filtered to sockets owned by the target wallet.
These snapshots are not a continuous process trace; no full trace or public-network
qualification is claimed. This opt-in replay requires Linux `/proc`.

## Replay

The lifecycle runner is published in Zinder PR #437 at immutable revision
`68866b2ed2caaa5d7ac7fb2c371cd3f6787ce34c`. Acquire its source separately from
the v0.21.0 service binaries:

```sh
git clone https://github.com/ZcashFoundation/zinder.git /absolute/path/to/runner
git -C /absolute/path/to/runner fetch origin refs/pull/437/head
git -C /absolute/path/to/runner checkout --detach 68866b2ed2caaa5d7ac7fb2c371cd3f6787ce34c
python3 qa/rpc-tests/native_wallet_lifecycle_manifest.py \
  --zallet-root /absolute/path/to/zallet \
  --runner-root /absolute/path/to/runner \
  --backend-binary /absolute/path/to/zallet-zinder \
  --ready-receipt /absolute/path/to/ready.json \
  --output /absolute/path/to/manifest.json
python3 /absolute/path/to/runner/scripts/run-wallet-lifecycle.py \
  --manifest /absolute/path/to/manifest.json \
  --output /absolute/path/to/new-evidence-directory
```

The published runner's `scripts/regtest-fixture.py` provisions isolated
fixtures; see its testing runbook for the readiness receipt. The receipt must
identify Zinder v0.21.0 revision `830b426060169f5b2e94d84ff0f134bc1ad96004`,
the native endpoint, and the mining RPC. The generator records the actual
Zallet revision; `--expected-zallet-revision` optionally enforces a caller's
source pin. It hashes both binaries, the interpreter, runner, adapter,
configuration, and fixture receipt. All validation remains enabled regardless
of Python assertions; optimized Python is rejected before wallet work. The
runner removes disposable wallet scratch after cleanup.

## Extended scenarios

`wallet_zinder_recovery.py` retains live account import/rescan, paginated
recovery, forced expiry, and shallow reorganization coverage. Its staged
artifacts are supplied through `ZIT_ZALLET_*`, `ZIT_ZINDER_*`, their `_SHA256`
variables, and `ZIT_ZALLET_REVISION`. These scenarios are separate from the
ordinary lifecycle replay; their historical receipts do not qualify a new
source revision.

The bounded-scan network scenarios live in `zinder_driver.rs` in this
repository. Build its external libtest executable against a Zallet checkout:

```sh
python3 qa/rpc-tests/build_zinder_driver.py \
  --zallet-root /absolute/path/to/zallet \
  --build-dir /absolute/path/to/driver-build
```

Supply the printed executable as `ZIT_ZALLET_ZINDER_TEST` to
`zinder_bounded_scan.py`, with its matching `_SHA256`. Zallet's
`bounded-scan-certification` feature enables only the scan seam and controlled
range barrier; no network tests run inside Zallet's unit suite.

For a bounded scan near the tip of an existing disposable chain, set
`ZIT_ZINDER_ENDPOINT`, `ZIT_ZALLET_CONFIG`, `ZIT_CERTIFICATION_DATADIR`,
`ZIT_CERTIFICATION_RESULT`, the backend executable `ZIT_ZALLET`, and the half-open range variables
`ZIT_REQUESTED_START_HEIGHT` and `ZIT_REQUESTED_END_HEIGHT_EXCLUSIVE`.
The range must contain two to twenty blocks and end immediately after the
visible tip. Before the first scan, invoke the printed driver with
`--exact chain::tests::initialize_bounded_scan_wallet --ignored`.
This Regtest-only initializer requires a nonexistent database and creates a
public deterministic account whose birthday matches the requested start,
using the endpoint's actual predecessor tree state. It initializes Zallet's database
through the CLI and does not scan blocks.
The existing full-chain scenarios initialize their own wallets through the CLI.
