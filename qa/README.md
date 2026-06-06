The [pull-tester](/pull-tester/) folder contains a script to call
multiple tests from the [rpc-tests](/rpc-tests/) folder.

Test dependencies
=================

Before running the tests, install the Python dependencies with `uv`:

```bash
uv sync
```

See the [`uv` installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
if it is not already installed.

Setup
=====

By default, binaries must exist in the `../src ` folder. All tests require the `zebrad`
binary; most tests require the `zallet` binary; some tests require the `zainod` binary.

Alternatively, you can set the binary paths with:
```
export ZEBRAD=/path/to/zebrad
export ZAINOD=/path/to/zainod
export ZALLET=/path/to/zallet
```

Running tests locally
=====================

You can run any single test by calling

    uv run ./qa/pull-tester/rpc-tests.py <testname1>

Or you can run any combination of tests by calling

    uv run ./qa/pull-tester/rpc-tests.py <testname1> <testname2> <testname3> ...

Run the regression test suite with

    uv run ./qa/pull-tester/rpc-tests.py

By default, tests will be run in parallel. To specify how many jobs to run,
append `--jobs=n` (default n=4).

If you want to create a basic coverage report for the RPC test suite, append `--coverage`.

Possible options, which apply to each individual test run:

```
  -h, --help            show this help message and exit
  --nocleanup           Leave zcashds and test.* datadir on exit or error
  --noshutdown          Don't stop zcashds after the test execution
  --srcdir=SRCDIR       Source directory containing zcashd/zcash-cli
                        (default: ../../src)
  --tmpdir=TMPDIR       Root directory for datadirs
  --tracerpc            Print out all RPC calls as they are made
  --coveragedir=COVERAGEDIR
                        Write tested RPC commands into this directory
```

If you set the environment variable `PYTHON_DEBUG=1` you will get some debug
output (example: `PYTHON_DEBUG=1 uv run ./qa/pull-tester/rpc-tests.py wallet`).

To get real-time output during a test you can run it using the
`uv run python3` such as:

```
uv run python3 qa/rpc-tests/wallet.py
```

A 200-block -regtest blockchain and wallets for four nodes
is created the first time a regression test is run and
is stored in the cache/ directory.  Each node has the miner
subsidy from 25 mature blocks (25*10=250 ZEC) in its wallet.

After the first run, the cache/ blockchain and wallets are
copied into a temporary directory and used as the initial
test state.

If you get into a bad state, you should be able
to recover with:

```bash
rm -rf cache
killall zebrad
killall zainod
killall zallet
```

Writing tests
=============
You are encouraged to write tests for new or existing features.
Further information about the test framework and individual RPC
tests is found in [rpc-tests](rpc-tests).
