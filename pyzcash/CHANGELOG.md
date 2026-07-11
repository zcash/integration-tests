# Changelog

All notable changes to pyzcash are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the major version is 0, the public API may change in any minor release.

## [Unreleased]

### Added

- Initial package scaffold: `pyzcash.errors`, the exception hierarchy that every
  failure raised by the library derives from.
- `pyzcash.encoding`, the codec layer: a typed `Reader`/`Writer` pair with
  canonical CompactSize, Base58Check, Bech32 and Bech32m, F4Jumble (both
  directions), and the personalized BLAKE2b/SHA-256 hashes the rest of the
  library is built on.
- `pyzcash.consensus`: `Network`, `NetworkUpgrade` with its consensus branch ID
  and per-network activation heights, `branch_id_for_height`, and `Zatoshi`, a
  range-checked amount type that refuses floats.
- `pyzcash.address`: transparent (P2PKH/P2SH), Sapling, unified (ZIP 316), and
  TEX (ZIP 320) addresses as a tagged union, with `parse_address` as the entry
  point. The ZIP 316 structural rules are enforced on decode.
- `pyzcash.script`: the `Opcode` enum, `Script` parsing and building, one
  canonical script-number codec, the standard templates (P2PKH, P2SH, OP_RETURN,
  multisig), and `script_pubkey_for` / `address_from_script_pubkey`.
- `pyzcash.transaction`: the v1 to v5 transaction model (ZIP 225), with the
  transparent, Sprout (both PHGR13 and Groth16 JoinSplits), Sapling, and Orchard
  bundles. Every transaction in the canonical ZIP 143, ZIP 243, and ZIP 244
  vectors parses and re-serializes byte for byte.

### Fixed

- F4Jumble split the message at `ceil(len / 2)` rather than the
  `min(floor(len / 2), 64)` ZIP 316 specifies. The two differ only for an odd
  length below 128, and the wrong split is still invertible, so it round-tripped
  cleanly and went unnoticed; the canonical unified-address vectors caught it.
