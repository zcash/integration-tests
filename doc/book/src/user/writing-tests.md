# Writing Tests

## Adding a new test

1. Add a new file `NEW_TEST.py` to the `qa/rpc-tests/` folder.
2. Update `qa/pull-tester/rpc-tests.py`, adding a new entry `'NEW_TEST.py',` to
   the `BASE_SCRIPTS` array, in the position matching how long the test takes to
   run — the list is ordered longest-first so that CI's shards stay balanced.
3. Write your test (either from scratch, or by modifying an existing test as
   appropriate).
4. Open a pull request with your changes.

If a test cannot pass yet because of a bug in `zebrad`, `zainod` or `zallet`,
add it to `DISABLED_SCRIPTS` as well, with a comment naming what it is waiting
on. It stays in `BASE_SCRIPTS` so it can still be run explicitly by name.

## Test framework

The test framework lives in `qa/rpc-tests/test_framework/`. Key modules:

| Module | Purpose |
|--------|---------|
| `test_framework.py` | `BitcoinTestFramework`, the base class every test derives from |
| `util.py` | Binary/port resolution, config generation, process lifecycle, sync and assertion helpers |
| `config.py` | `ZebraArgs` / `ZalletArgs`, the typed way to configure a node or wallet |
| `proxy.py`, `authproxy.py` | JSON-RPC clients (zebrad and zainod; zallet, respectively) |
| `util_ironwood.py` | Shared setup for the Ironwood (NU6.3) scenario tests |
| `zcashd_migration.py` | Drives `zallet migrate-zcashd-wallet` against the `test-wallet/` fixture |
| `ufvk_decode.py` | Decoding for unified containers (UA / UFVK / UIVK) |
| `mininode.py` | Serialization for `CBlock`, `CTransaction` and friends |

## Structuring a test

A test subclasses `BitcoinTestFramework` and declares what it needs in
`__init__`:

```python
class MyTest(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.num_nodes = 1     # zebrad instances
        self.num_wallets = 1   # zallet instances
        self.num_indexers = 0  # zainod instances
        self.cache_behavior = 'clean'
```

`setup_network()` then starts each daemon, connects the nodes to each other, and
waits for them to sync, before calling your `run_test()`. Reach the daemons
through `self.nodes[i]` (zebrad), `self.wallets[i]` (zallet) and
`self.zainos[i]` (zainod).

### Chain initialization

`cache_behavior` selects how the chain starts:

| Value | Behaviour |
|-------|-----------|
| `'clean'` | No chain data; the test mines everything it needs. Most tests use this. |
| `'current'` | Copy in a shared 200-block chain and wallets, building the cache first if it is missing. The default. |
| `'fresh'` | Rebuild that cache, then start as for `'current'`. |

### Configuring nodes and wallets

Pass a `ZebraArgs` or `ZalletArgs` rather than command-line strings — the
framework renders them into the daemon's config file:

```python
def setup_nodes(self):
    return start_nodes(self.num_nodes, self.options.tmpdir,
                       extra_args=[ZebraArgs(activation_heights={'NU6': 10})])
```

## Backends

`zallet` is a launcher that execs a per-backend binary. The suite runs against
both: set `ZALLET_BACKEND=zebra` to exercise the zebra backend instead of the
default `zaino`. CI runs every shard under both backends, so a test must pass
under each.
