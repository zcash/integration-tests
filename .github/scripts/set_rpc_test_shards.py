#!/usr/bin/env python3
"""Assign RPC tests to shards and emit the shard matrix as JSON.

Extracted from the inline ./subclass.py heredoc in the "set-rpc-tests" job
of .github/workflows/ci.yml. Prints a JSON list of {shard, rpc_tests}
objects on stdout.

Run from the repository root. Configuration is read from the environment:

    SRC_DIR             Source directory (unused here, kept for parity).
    IS_INTEROP_REQUEST  "true" to skip the not-yet-passing test set.
"""

import importlib
import json
import os
import sys

sys.path.append('qa/pull-tester')
rpc_tests = importlib.import_module('rpc-tests')

src_dir = os.environ["SRC_DIR"]
is_interop_request = os.environ["IS_INTEROP_REQUEST"] == "true"
SHARDS = 10

# While we are getting the existing tests to pass, skip tests that are
# not expected to pass; this makes it easier to distinguish expected
# failures from unexpected ones.
if is_interop_request:
    old_shards = 0
else:
    num_old_tests = len(rpc_tests.BASE_SCRIPTS + rpc_tests.ZMQ_SCRIPTS)
    num_new_tests = len(rpc_tests.NEW_SCRIPTS)
    old_shards = (num_old_tests * SHARDS) // (num_old_tests + num_new_tests)

new_shards = SHARDS - old_shards

# These tests are ordered longest-test-first, to favor running tests in
# parallel with the regular test runner. For chunking purposes, assign
# tests to shards in round-robin order.
test_shards = {}
if old_shards > 0:
    for i, test in enumerate(rpc_tests.BASE_SCRIPTS + rpc_tests.ZMQ_SCRIPTS):
        test_shards.setdefault(i % old_shards, []).append(test)
for i, test in enumerate(rpc_tests.NEW_SCRIPTS):
    test_shards.setdefault(old_shards + (i % new_shards), []).append(test)

test_list = []
for i, tests in test_shards.items():
    test_list.append({
        'shard': 'shard-%d' % i,
        'rpc_tests': tests,
    })

# These tests involve enough shielded spends (consuming all CPU cores)
# that we can't run them in parallel, or fail intermittently so we run
# them separately to enable not requiring that they pass.
if not is_interop_request:
    for test in rpc_tests.SERIAL_SCRIPTS + rpc_tests.FLAKY_SCRIPTS:
        test_list.append({
            'shard': test,
            'rpc_tests': [test],
        })

print(json.dumps(test_list))
