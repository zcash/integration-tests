# Running Tests

## Prerequisites

### Binaries

All tests require the `zebrad` binary; most tests require the `zallet` binary;
some tests require the `zainod` binary. The gRPC parity tests additionally
require the `lightwalletd` binary.

By default, binaries must exist in the `./src/` folder under the repository
root. Alternatively, you can set the binary paths with environment variables:

```bash
export ZEBRAD=/path/to/zebrad
export ZAINOD=/path/to/zainod
export ZALLET=/path/to/zallet
export LIGHTWALLETD=/path/to/lightwalletd
```

### Python dependencies

The `zmq`, `toml`, `base58`, `grpcio`, and `protobuf` Python libraries are required.

```bash
uv sync
```

See the [`uv` installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
if it is not already installed.

## Running the full test suite

```bash
uv run ./qa/zcash/full_test_suite.py
```

## Running the gRPC parity tests

The gRPC parity tests run [`zainod`] and [`lightwalletd`] side-by-side against
the same [`zebrad`] node and compare their [lightwallet-protocol] gRPC responses.
They require the `lightwalletd` binary (see [Binaries](#binaries) above).

```bash
uv run ./qa/zcash/grpc_comparison_tests.py
```

Pass any [test runner options](#test-runner-options) after the script name:

```bash
uv run ./qa/zcash/grpc_comparison_tests.py --nocleanup
```

[`zebrad`]: https://github.com/ZcashFoundation/zebra
[`zainod`]: https://github.com/zingolabs/zaino
[`lightwalletd`]: https://github.com/zcash/lightwalletd
[lightwallet-protocol]: https://github.com/zcash/lightwallet-protocol

## Running individual tests

Run a single test:

```bash
uv run ./qa/pull-tester/rpc-tests.py <testname>
```

Run multiple specific tests:

```bash
uv run ./qa/pull-tester/rpc-tests.py <testname1> <testname2> <testname3>
```

Run all regression tests:

```bash
uv run ./qa/pull-tester/rpc-tests.py
```

## Parallel execution

By default, tests run in parallel with 4 jobs. To change the number of jobs:

```bash
uv run ./qa/pull-tester/rpc-tests.py --jobs=n
```

## Test runner options

```
-h, --help            show this help message and exit
--nocleanup           Leave test datadirs on exit or error
--noshutdown          Don't stop nodes after the test execution
--srcdir=SRCDIR       Source directory containing binaries (default: ../../src)
--tmpdir=TMPDIR       Root directory for datadirs
--tracerpc            Print out all RPC calls as they are made
--coveragedir=COVERAGEDIR
                      Write tested RPC commands into this directory
```

## Debugging

Set `PYTHON_DEBUG=1` for debug output:

```bash
PYTHON_DEBUG=1 uv run ./qa/pull-tester/rpc-tests.py wallet
```

For real-time output, run a test directly with `uv run python3`:

```bash
uv run python3 qa/rpc-tests/wallet.py
```

## Cache management

A 200-block regtest blockchain and wallets for four nodes are created the first
time a regression test is run and stored in the `cache/` directory. Each node has
the miner subsidy from 25 mature blocks (25*10=250 ZEC) in its wallet.

After the first run, the cached blockchain and wallets are copied into a
temporary directory and used as the initial test state.

If you get into a bad state, you can recover with:

```bash
rm -rf cache
killall zebrad
killall zainod
killall zallet
killall lightwalletd
```
