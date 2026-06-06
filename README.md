Zcash Integration Tests
===========

<!-- ANCHOR: summary -->
This repository hosts integration tests and associated CI infrastructure for the
Zcash ecosystem. The following tests are provided:

- Functional tests in Python of [`zebrad`], [`zainod`], and [`zallet`], using
  regtest mode and primarily their JSON-RPC interfaces.
- gRPC parity tests that run [`zainod`] and [`lightwalletd`] side-by-side
  against the same [`zebrad`] node and compare their
  [lightwallet-protocol] gRPC responses.

The functional tests and CI workflows were originally part of the [`zcashd`]
codebase, with the Python test framework (and some of the tests) inherited from
[Bitcoin Core].

[`zebrad`]: https://github.com/ZcashFoundation/zebra
[`zainod`]: https://github.com/zingolabs/zaino
[`zallet`]: https://github.com/zcash/wallet
[`lightwalletd`]: https://github.com/zcash/lightwalletd
[lightwallet-protocol]: https://github.com/zcash/lightwallet-protocol
[`zcashd`]: https://github.com/zcash/zcash
[Bitcoin Core]: https://github.com/bitcoin/bitcoin
<!-- ANCHOR_END: summary -->

## Getting Started

### Running the tests locally
Pre-requisite: See the [`uv` installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
  if it is not already installed.

- Clone the repository.
- Build `zebrad`, `zainod`, and `zallet` binaries, and place them in a folder
  `./src/` under the repository root.
- `uv sync`
- `uv run ./qa/zcash/full_test_suite.py`

See [the README for the functional tests][qa/README.md] for additional usage
information.

### Running the gRPC parity tests

The gRPC parity tests additionally require the `lightwalletd` binary in `./src/`
(or set `LIGHTWALLETD=/path/to/lightwalletd`).

```bash
uv run ./qa/zcash/grpc_comparison_tests.py
```

### Writing tests

- For new tests:
  - Add a new file `NEW_TEST.py` to the `qa/rpc-tests` folder.
  - Update `qa/pull-tester/rpc-tests.py`, adding a new entry `'NEW_TEST.py',` to
    the `BASE_SCRIPTS` array (either at the end of the array, or in the
    appropriate position based on how long the test takes to run).
- Write your test (either new from scratch, or making changes to an existing
  test as appropriate).
- Open a pull request with your changes.

## Cross-Repository CI Integration

This repository supports triggering integration tests from PRs in external
repositories (including those in other GitHub organizations) and reporting
results back as status checks. See [doc/cross-repo-ci.md](doc/book/ci/cross-repo.md)
for setup instructions.

Participation in the Zcash project is subject to a
[Code of Conduct](code_of_conduct.md).

License
-------

For license information see the file [COPYING](COPYING).
