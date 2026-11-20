# Native Zinder 0.21.0 lifecycle replay

This layered scenario exercises the real Zallet launcher and `zallet-zinder`
process against an externally provisioned, disposable Regtest stack. It uses
Zinder's public native WalletQuery interface, never the compatibility service.
The manifest generator pins Zinder v0.21.0 revision
`830b426060169f5b2e94d84ff0f134bc1ad96004` and the locally integrated Zallet
revision `38ef6a9b344063ca17b9989eeab22b68a82b6394`.

Build the root `zallet` launcher with the repository toolchain and build
`backends/zinder` with Rust 1.100 and `bounded-scan-certification`. The backend
pins released `zinder-client` and test-only `zinder-core` 0.21.0. The endpoint
must retain full blocks, advertise all 16 required capabilities, and match the
adapter's upgrade schedule: Overwinter through Canopy at 1, NU5 at 2, NU6 at 3,
NU6.1 at 4, NU6.2 at 5, NU6.3 at 6, and NU7 at 140.

Only run against a dedicated chain where mining is authorized. The adapter
accepts loopback HTTP endpoints, generates disposable wallet material, mines
coinbases, shields one matured output into Ironwood, and sends a private
self-transfer. It checks native pending observation, mining, scan-back, a real
process restart, and the mined transaction after resume. The producer is first funded using a public BIP 39 test mnemonic. The restore
step imports that recovery material into a new independent database, verifies
the same public keys and preexisting shielded transaction, and preserves the
original producer database. Resume mines and scans a fresh block after restart.
Reports use always-on validation, reject optimized Python, and bind the same
transaction fingerprint through submission, wallet pending, canonical mining,
and wallet scan-back. Endpoint attribution is configured-native-only; this
adapter does not collect socket traces. No external funds, certificates, or production state are used.

Generate an input-bound manifest, then run the shared lifecycle runner from
Zinder's `wallet-lifecycle-test-gate` integration layer:

```sh
python3 qa/rpc-tests/native_wallet_lifecycle_manifest.py \
  --zallet-root /absolute/path/to/zallet \
  --ready-receipt /absolute/path/to/ready-all.json \
  --output /absolute/path/to/native-manifest.json
python3 /absolute/path/to/zinder/scripts/run-wallet-lifecycle.py \
  --manifest /absolute/path/to/native-manifest.json \
  --output /absolute/path/to/new-evidence-directory
```

The ready receipt names the exact release revision, native endpoint, and mining
RPC. The manifest hashes both binaries, the Python interpreter, adapter,
generator, default configuration, test encryption fixture, and receipt. The
runner retains step assertions and removes its disposable wallet scratch.

This is a current lifecycle replacement layer for the old v0.6 process
composition in `wallet_zinder_recovery.py`. It does not claim that script's
reorganization, cross-page history, bounded-scan, or forced-expiry scenarios
have been refreshed. Forced production expiry, hot account creation, foreign
Sapling viewing-key import, and existing-key rescan have separate local receipts
in the consumer qualification evidence. The upstream PR remains read-only.
