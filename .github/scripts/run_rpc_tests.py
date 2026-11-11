#!/usr/bin/env python3
#
# Run one CI shard of RPC tests, all of them in parallel, and exit non-zero if
# any test fails. Used by the "RPC test" step in `ci.yml`.
#
# Environment:
#   RPC_TESTS  JSON list of test scripts (with arguments) to run, as produced
#              by `set_rpc_test_shards.py`.
#   SRC_DIR    Repository root containing `qa/rpc-tests/`.
#   EXEEXT     Executable suffix of the node binaries (empty except on Windows).
#
# The binaries are found through the ZEBRAD, ZAINOD and ZALLET environment
# variables, or under `./src/` when those are unset.

import importlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO_ROOT / "qa" / "pull-tester"))
rpc_tests = importlib.import_module("rpc-tests")

# The test framework resolves `src/` and `qa/cache` relative to this.
BUILD_DIR = "."


def parse_test_list(raw: str) -> list[str]:
    """Decode RPC_TESTS, rejecting anything but a non-empty list of strings."""
    tests = json.loads(raw)
    if (
        not isinstance(tests, list)
        or not tests
        or not all(isinstance(t, str) for t in tests)
    ):
        msg = f"RPC_TESTS must be a non-empty JSON list of strings: {raw!r}"
        raise ValueError(msg)
    return tests


def main() -> None:
    test_list = parse_test_list(os.environ["RPC_TESTS"])
    src_dir = os.environ["SRC_DIR"]
    exeext = os.environ.get("EXEEXT", "")

    all_passed = rpc_tests.run_tests(
        rpc_tests.RPCTestHandler,
        test_list,
        src_dir,
        BUILD_DIR,
        exeext,
        jobs=len(test_list),
    )
    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
