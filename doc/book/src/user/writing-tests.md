# Writing Tests

## Adding a new test

1. Add a new file `NEW_TEST.py` to the `qa/rpc-tests/` folder.
2. Update `qa/pull-tester/rpc-tests.py`, adding a new entry `'NEW_TEST.py',` to
   the `NEW_SCRIPTS` array (either at the end of the array, or in the
   appropriate position based on how long the test takes to run).
3. Write your test (either from scratch, or by modifying an existing test as
   appropriate).
4. Open a pull request with your changes.

## Test framework

The test framework lives in `qa/rpc-tests/test_framework/`. Key modules:

| Module | Purpose |
|--------|---------|
| `test_framework.py` | Base class for RPC regression tests |
| `util.py` | Generally useful test utility functions |
| `mininode.py` | P2P connectivity support |
| `comptool.py` | Framework for comparison-tool style P2P tests |
| `script.py` | Utilities for manipulating transaction scripts |
| `blockstore.py` | Disk-backed block and tx storage |
| `key.py` | Wrapper around OpenSSL EC_Key |
| `bignum.py` | Helpers for `script.py` |
| `blocktools.py` | Helper functions for creating blocks and transactions |
| `proto/` | Generated Python gRPC stubs for the [lightwallet-protocol] |

[lightwallet-protocol]: https://github.com/zcash/lightwallet-protocol

## Writing gRPC parity tests

The framework supports starting a `lightwalletd` instance alongside a `zainod`
instance and comparing their `CompactTxStreamer` gRPC responses. See
`qa/rpc-tests/grpc_comparison.py` for a complete example.

### Service lifecycle

Set `num_lightwalletds` in your test's `__init__` alongside `num_indexers`:

```python
class MyGrpcTest(BitcoinTestFramework):
    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_indexers = 1       # starts zainod
        self.num_lightwalletds = 1  # starts lightwalletd
        self.num_wallets = 0
        self.cache_behavior = 'clean'
```

After `setup_network()` runs, `self.lwds` holds a list of gRPC port numbers
(one per lightwalletd instance). `self.zainos` holds JSON-RPC proxy objects as
usual, but the Zainod gRPC port is obtained via `zaino_grpc_port(i)`.

### Connecting gRPC clients

```python
import grpc
from test_framework.proto import service_pb2, service_pb2_grpc
from test_framework.util import zaino_grpc_port

zainod_ch = grpc.insecure_channel(f"127.0.0.1:{zaino_grpc_port(0)}")
lwd_ch    = grpc.insecure_channel(f"127.0.0.1:{self.lwds[0]}")

zs = service_pb2_grpc.CompactTxStreamerStub(zainod_ch)
ls = service_pb2_grpc.CompactTxStreamerStub(lwd_ch)
```

### Regenerating the proto stubs

The proto files live in the `lightwallet-protocol/` git subtree
(`zcash/lightwallet-protocol`). To update to a new protocol version:

```bash
# Pull the new version
git subtree pull --prefix=lightwallet-protocol \
  https://github.com/zcash/lightwallet-protocol.git <NEW_VERSION> --squash

# Regenerate Python stubs (requires grpcio-tools: uv tool install grpcio-tools)
scripts/generate_proto.sh

# Commit both the subtree update and the regenerated stubs
git add lightwallet-protocol/ qa/rpc-tests/test_framework/proto/
git commit
```

## P2P test design

### Mininode

`mininode.py` contains all the definitions for objects that pass over the
network (`CBlock`, `CTransaction`, etc., along with the network-level wrappers
`msg_block`, `msg_tx`, etc.).

P2P tests have two threads: one handles all network communication with the nodes
being tested (using Python's asyncore package); the other implements the test
logic.

`NodeConn` is the class used to connect to a node. Implement a callback class
that derives from `NodeConnCB` and pass it to the `NodeConn` object to receive
callbacks when events of interest arrive. Be sure to call
`self.create_callback_map()` in your derived class's `__init__` function.

Call `NetworkThread.start()` after all `NodeConn` objects are created to start
the networking thread, then continue with the test logic in your existing thread.

RPC calls are also available in P2P tests.

### Comptool

Comptool is a testing framework for writing tests that compare the block/tx
acceptance behavior of a node against one or more other node instances, or
against known outcomes, or both.

Set the `num_nodes` variable (defined in `ComparisonTestFramework`) to start up
one or more nodes. Implement a generator function called `get_tests()` which
yields `TestInstance`s. Each `TestInstance` consists of:

- A list of `[object, outcome, hash]` entries, where:
  - `object` is a `CBlock`, `CTransaction`, or `CBlockHeader`.
  - `outcome` is `True`, `False`, or `None`.
  - `hash` (optional) is the block hash of the expected tip.
- `sync_every_block`: if `True`, each block is tested in sequence and synced; if
  `False`, all blocks are inv'd together and only the last block is tested.
- `sync_every_transaction`: analogous to `sync_every_block` for transactions.

For examples, see `invalidblockrequest.py` and `p2p-fullblocktest.py`.
