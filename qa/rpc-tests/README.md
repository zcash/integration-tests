Regression tests
================

Each `*.py` file in this directory is one test, driven by
[`qa/pull-tester/rpc-tests.py`](../pull-tester/rpc-tests.py). See
[`qa/README.md`](../README.md) for how to run them, and the
[book](../../doc/book/src/user/writing-tests.md) for how to write one.

Test framework
--------------

### [test_framework/test_framework.py](test_framework/test_framework.py)
`BitcoinTestFramework`, the base class every test derives from. A test declares
how many of each daemon it wants (`num_nodes` zebrads, `num_wallets` zallets,
`num_indexers` zainods) and how the chain should be initialized
(`cache_behavior`); the base class starts them, wires them together, and tears
them down.

### [test_framework/util.py](test_framework/util.py)
Binary and port resolution, config-file generation, process lifecycle, and
chain/wallet sync helpers for each daemon, plus the assertion helpers.

### [test_framework/config.py](test_framework/config.py)
`ZebraArgs` and `ZalletArgs`, the typed way to configure a node or wallet
(activation heights, funding streams, miner address). Prefer these over raw
command-line strings.

### [test_framework/proxy.py](test_framework/proxy.py) and [test_framework/authproxy.py](test_framework/authproxy.py)
JSON-RPC clients. `proxy.py` is used for zebrad and zainod, which run with
cookie auth disabled in regtest; `authproxy.py` adds HTTP basic auth and is
used for zallet.

### [test_framework/util_ironwood.py](test_framework/util_ironwood.py)
Shared setup and helpers for the Ironwood (NU6.3) scenario tests.

### [test_framework/zcashd_migration.py](test_framework/zcashd_migration.py)
Drives `zallet migrate-zcashd-wallet` against the checked-in
[`test-wallet/`](test-wallet/) fixture, for the tests that cover importing a
legacy zcashd `wallet.dat`.

### [test_framework/ufvk_decode.py](test_framework/ufvk_decode.py)
Decoding for unified containers (UA / UFVK / UIVK).

### [test_framework/mininode.py](test_framework/mininode.py)
Serialization for the objects that pass over the network (`CBlock`,
`CTransaction`, and friends). Tests use it to parse raw transactions;
[test_framework/equihash.py](test_framework/equihash.py),
[test_framework/zip244.py](test_framework/zip244.py),
[test_framework/script.py](test_framework/script.py) and
[test_framework/bignum.py](test_framework/bignum.py) support it.

### [test_framework/coverage.py](test_framework/coverage.py)
Wraps an RPC proxy to record which RPC methods a run exercised, for
`rpc-tests.py --coverage`.
