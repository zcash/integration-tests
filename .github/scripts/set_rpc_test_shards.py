#!/usr/bin/env python3
#
# Assign the RPC tests listed in `qa/pull-tester/rpc-tests.py` to CI shards,
# and print the result as a JSON list of `{"shard": ..., "rpc_tests": [...]}`
# objects for the RPC test job matrix in `ci.yml`.
#
# Environment:
#   IS_INTEROP_REQUEST  "true" when CI was triggered by a tested project's
#                       `*-interop-request`; only the new tests run then.

import importlib
import json
import os
import sys
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO_ROOT / "qa" / "pull-tester"))
rpc_tests = importlib.import_module("rpc-tests")

# Number of shards the parallel tests are split across. Serial and flaky tests
# get one shard each, on top of these.
SHARDS = 10


class Shard(TypedDict):
    shard: str
    rpc_tests: list[str]


def assign_shards(is_interop_request: bool) -> list[Shard]:
    old_tests: list[str] = rpc_tests.BASE_SCRIPTS + rpc_tests.ZMQ_SCRIPTS
    new_tests: list[str] = rpc_tests.NEW_SCRIPTS

    # While we are getting the existing tests to pass, skip tests that are not
    # expected to pass; this makes it easier to distinguish expected failures
    # from unexpected ones.
    if is_interop_request or not old_tests:
        old_shards = 0
    elif not new_tests:
        old_shards = SHARDS
    else:
        # Split the shards in proportion to the test counts, but give each
        # non-empty group at least one shard. Without the clamp, a group much
        # smaller than the other rounds down to zero shards and its tests are
        # silently dropped (one old test against 30 new ones gives 10 // 31).
        proportional = (len(old_tests) * SHARDS) // (
            len(old_tests) + len(new_tests)
        )
        old_shards = min(SHARDS - 1, max(1, proportional))

    new_shards = SHARDS - old_shards

    # These tests are ordered longest-test-first, to favor running tests in
    # parallel with the regular test runner. For chunking purposes, assign
    # tests to shards in round-robin order.
    test_shards: dict[int, list[str]] = {}
    if old_shards > 0:
        for i, test in enumerate(old_tests):
            test_shards.setdefault(i % old_shards, []).append(test)
    for i, test in enumerate(new_tests):
        test_shards.setdefault(old_shards + (i % new_shards), []).append(test)

    test_list: list[Shard] = [
        {"shard": f"shard-{i}", "rpc_tests": tests}
        for i, tests in test_shards.items()
    ]

    # These tests involve enough shielded spends (consuming all CPU cores) that
    # we can't run them in parallel, or fail intermittently so we run them
    # separately to enable not requiring that they pass.
    if not is_interop_request:
        serial: list[str] = rpc_tests.SERIAL_SCRIPTS + rpc_tests.FLAKY_SCRIPTS
        test_list.extend(
            {"shard": test, "rpc_tests": [test]} for test in serial
        )

    return test_list


def main() -> None:
    is_interop_request = os.environ.get("IS_INTEROP_REQUEST") == "true"
    print(json.dumps(assign_shards(is_interop_request)))


if __name__ == "__main__":
    main()
