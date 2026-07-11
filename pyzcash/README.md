# pyzcash

Typed Zcash protocol primitives for Python: parse and encode addresses,
scripts, and transactions, and compute the digests the consensus rules define.

`pyzcash` is dependency-free and offline. It reads and writes Zcash's wire
formats; it does not talk to a node, verify proofs, or decide what is valid.

```python
from pyzcash import Network
from pyzcash.address import parse_address, UnifiedAddress, ReceiverType

address = parse_address("u1...", Network.MAIN)
match address:
    case UnifiedAddress() if address.has_orchard:
        orchard = address.receiver(ReceiverType.ORCHARD)
```

## Status

Early. The package is being extracted, layer by layer, from the Zcash
integration test framework (`qa/rpc-tests/test_framework/`), which has carried
this logic for years as untyped, test-only code. Each layer lands with types,
tests, and published test vectors, and the test framework is migrated onto it,
so the integration suite acts as the regression net for the extraction.

Landed:

- `pyzcash.errors` - the exception hierarchy every failure derives from.
- `pyzcash.encoding` - CompactSize, Base58Check, Bech32/Bech32m, F4Jumble, and
  the hash functions (SHA-256d, personalized BLAKE2b).
- `pyzcash.consensus` - networks, network upgrades, branch IDs, activation
  heights, and a range-checked `Zatoshi` amount type.
- `pyzcash.address` - transparent (P2PKH, P2SH), Sapling, Unified (ZIP 316),
  and TEX (ZIP 320) addresses, as a parsed tagged union.

Planned, in dependency order:

- `pyzcash.script` - opcodes, script parsing, and the standard templates.
- `pyzcash.transaction` - the v1 through v5 transaction model (ZIP 225), with
  the transparent, Sprout, Sapling, and Orchard bundles.
- `pyzcash.digest` - txid and auth digests, and sighash (ZIP 143/243/244).
- `pyzcash.fees` - the ZIP 317 conventional fee.

Signing (secp256k1) and a typed RPC client are deliberately out of the core and
will arrive as optional extras, so that parsing a transaction never pulls in a
compiler toolchain.

## Requirements

Python 3.12 or newer. No runtime dependencies.

## Development

```sh
make setup      # install with dev dependencies
make check      # formatting, lint, mypy --strict, and tests
```

Every public symbol is typed and the package ships `py.typed`, so downstream
`mypy` sees the annotations.

## License

MIT. See `LICENSE`.
