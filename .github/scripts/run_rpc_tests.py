#!/usr/bin/env python3
"""Run a list of RPC tests using a custom test handler.

Extracted from the inline ./subclass.py heredoc in the "RPC test" job of
.github/workflows/ci.yml. It subclasses rpc-tests.RPCTestHandler so test
subprocesses inherit the caller's environment unchanged.

Run from the repository root. Configuration is read from the environment:

    SRC_DIR    Source directory passed through to run_tests (required).
    EXEEXT     Executable file extension, e.g. ".exe" on Windows.
    RPC_TESTS  JSON-encoded list of test names to run (required).

Exits with status 1 if any test fails.
"""

import importlib
import json
import os
import subprocess
import sys

sys.path.append('qa/pull-tester')
rpc_tests = importlib.import_module('rpc-tests')

src_dir = os.environ["SRC_DIR"]
build_dir = '.'
exeext = os.environ["EXEEXT"]


class MyTestHandler(rpc_tests.RPCTestHandler):
    def start_test(self, args, stdout, stderr):
        return subprocess.Popen(
            args,
            universal_newlines=True,
            stdout=stdout,
            stderr=stderr)


test_list = json.loads(os.environ["RPC_TESTS"])
all_passed = rpc_tests.run_tests(MyTestHandler, test_list, src_dir, build_dir, exeext, jobs=len(test_list))
if all_passed == False:
    sys.exit(1)
