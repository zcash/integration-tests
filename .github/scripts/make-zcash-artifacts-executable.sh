#!/usr/bin/env bash
#
# Make the zcash binaries downloaded into ./src executable on the test runners.
#
# The `zallet` launcher execs a sibling backend binary (`zallet-zaino` or
# `zallet-zebra`), so those are made executable too when present.
#
# Inputs (environment):
#   FILE_EXT  executable suffix, e.g. ".exe" (optional, default empty)

set -euo pipefail

ext="${FILE_EXT:-}"

chmod +x "./src/zebrad$ext"
chmod +x "./src/zainod$ext"
chmod +x "./src/zallet$ext"
if [ -f "./src/zallet-zaino$ext" ]; then
    chmod +x "./src/zallet-zaino$ext"
fi
if [ -f "./src/zallet-zebra$ext" ]; then
    chmod +x "./src/zallet-zebra$ext"
fi
