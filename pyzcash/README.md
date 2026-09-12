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
- `pyzcash.script` - opcodes, script parsing and building, the standard
  templates, and the translation between scripts and transparent addresses.

- `pyzcash.transaction` - the v1 through v5 transaction model (ZIP 225), with
  the transparent, Sprout, Sapling, and Orchard bundles.
- `pyzcash.digest` - txids, auth digests, and sighashes (ZIP 143, ZIP 243,
  ZIP 244).
- `pyzcash.fees` - the ZIP 317 conventional fee.

Signing (secp256k1) and a typed RPC client are deliberately out of the core and
will arrive as optional extras, so that parsing a transaction never pulls in a
compiler toolchain.

## Requirements

**To use the library:** Python 3.12 or newer. Nothing else; there are no runtime
dependencies, by design. Parsing a transaction should never pull in a compiler
toolchain.

**To develop it:** [uv](https://docs.astral.sh/uv/) (which installs the right
Python for you) and `make`. Everything else comes from `make setup`.

```sh
make setup      # install the package and its dev dependencies
make check      # everything CI runs: format, lint, mypy --strict, tests
```

The dev dependencies are `ruff` (format and lint), `mypy` (type check), `pytest`
(tests), `hypothesis` (property-based tests), and `base58` (the reference
implementation this library's own Base58 is differential-tested against). None
of them is a runtime dependency of the shipped package.

## Layout

```
src/pyzcash/          the library, one package per layer (see Status above)
tests/
  test_*.py           unit tests, one module per layer
  properties/         property-based tests, one module per primitive
  vectors.py          hand-picked vectors: real regtest keys, addresses, and
                      scripts taken from the integration suite's fixtures
  vectors_json/       the canonical zcash-test-vectors corpus, vendored
                      verbatim and pinned to the commit in vectors_json/COMMIT
  json_vectors.py     loader for that corpus
  strategies.py       Hypothesis strategies (generators of arbitrary valid values)
  conftest.py         shared pytest fixtures
```

## Testing

Three kinds of test, which catch different things. None of them replaces
another.

**Unit tests** (`tests/test_*.py`) pin the behaviour of each layer, including
the errors. Run one module with the command in its header, for example:

```sh
uv run pytest tests/test_address.py
```

**Canonical vectors** (`tests/test_canonical_vectors.py`,
`tests/test_transaction.py`) check this implementation against
[zcash-test-vectors](https://github.com/zcash/zcash-test-vectors), the corpus
librustzcash, the sapling and orchard crates, and zcashd all test against.
Agreeing with it is evidence that this reads Zcash *correctly*, rather than
merely consistently with itself. That distinction is not academic: these vectors
found a real F4Jumble bug that every round-trip test in the suite had happily
passed, because the wrong permutation was still invertible.

**Property tests** (`tests/properties/`) assert invariants over generated
inputs, so they cover the cases nobody thought of. They check round-tripping,
canonicality (one value, one encoding, or a transaction would have two hashes),
and robustness: every parser handed arbitrary bytes either succeeds or raises a
`ZcashError`, never an `IndexError` leaking through the abstraction. The
transaction fuzzer mutates real Sapling and Orchard transactions from the
canonical corpus, so the parser is exercised all the way into the bundles.
These found two genuine bugs; see `tests/properties/test_script.py`.

```sh
uv run pytest tests/properties/    # all the property tests
```

## Typing

Every public symbol is typed and the package ships `py.typed`, so downstream
`mypy` sees the annotations. The library contains **no** `type: ignore` and mypy
has no per-module exceptions. `tests/test_typing_discipline.py` enforces both,
because this library is meant to be read, and a reader has to be able to trust
that the types say what the code does.

## License

MIT. See `LICENSE`.
