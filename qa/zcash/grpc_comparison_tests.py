#!/usr/bin/env python3
#
# Run the gRPC parity tests comparing Zainod and Lightwalletd
# backed by the same Zebrad node.
#
# Usage:
#   uv run ./qa/zcash/grpc_comparison_tests.py [rpc-tests options]
#
# Examples:
#   uv run ./qa/zcash/grpc_comparison_tests.py
#   uv run ./qa/zcash/grpc_comparison_tests.py --nocleanup
#
# Binaries are resolved from ./src/ by default, or from environment variables:
#   ZEBRAD, ZAINOD, LIGHTWALLETD
#

import os
import subprocess
import sys

REPOROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__)
        )
    )
)

def repofile(filename):
    return os.path.join(REPOROOT, filename)

def main():
    cmd = [repofile('qa/pull-tester/rpc-tests.py'), 'grpc_comparison.py'] + sys.argv[1:]
    sys.exit(subprocess.call(cmd))

if __name__ == '__main__':
    main()
