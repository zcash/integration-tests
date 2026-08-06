Zcash Integration Tests
===========

<!-- ANCHOR: summary -->
This repository hosts integration tests and associated CI infrastructure for the
Zcash ecosystem. The following tests are provided:

- Functional tests in Python of [`zebrad`], [`zainod`], and [`zallet`], using
  regtest mode and primarily their JSON-RPC interfaces.

The functional tests and CI workflows were originally part of the [`zcashd`]
codebase, with the Python test framework (and some of the tests) inherited from
[Bitcoin Core]. `zcashd` has since reached end of life and been archived, and
nothing here builds or runs it; what remains of it is the migration path off it,
covered by the tests that exercise `zallet migrate-zcashd-wallet`.

[`zebrad`]: https://github.com/ZcashFoundation/zebra
[`zainod`]: https://github.com/zingolabs/zaino
[`zallet`]: https://github.com/zcash/zallet
[`zcashd`]: https://github.com/zcash/zcash
[Bitcoin Core]: https://github.com/bitcoin/bitcoin
<!-- ANCHOR_END: summary -->

## Getting Started

### Running the tests locally

- Clone the repository.
- Build `zebrad`, `zainod`, and `zallet` binaries, and place them in a folder
  `./src/` under the repository root. `zallet` is a thin launcher that execs a
  per-backend binary, so also build the backend binaries (`zallet-zaino`, and
  `zallet-zebra` to exercise the zebra backend) and place them in `./src/`
  alongside the launcher (the launcher looks for the backend binary next to
  itself). The launcher selects the backend from the top-level `backend` key in
  `qa/defaults/zallet/zallet.toml` (default `zaino`); set `ZALLET_BACKEND=zebra`
  to run the suite against the zebra backend instead. The zebra backend reads
  the co-located zebrad's state database directly, so it needs a `zebrad` built
  with the (non-default) `indexer` feature.

#### With uv (recommended)

- `uv sync`
- `uv run ./qa/zcash/full_test_suite.py`

#### Without uv

On Ubuntu or Debian-based distributions:
- `sudo apt-get install python3-toml python3-embit`
- `./qa/zcash/full_test_suite.py`

On macOS or other platforms:
- `python3 -m venv venv`
- `. venv/bin/activate`
- `pip3 install toml embit`
- `./qa/zcash/full_test_suite.py`

See [the README for the functional tests][qa/README.md] for additional usage
information.

### Writing tests

- For new tests:
  - Add a new file `NEW_TEST.py` to the `qa/rpc-tests` folder.
  - Update `qa/pull-tester/rpc-tests.py`, adding a new entry `'NEW_TEST.py',` to
    the `BASE_SCRIPTS` array, in the position matching how long the test takes
    to run (the list is ordered longest-first to keep CI's shards balanced).
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
