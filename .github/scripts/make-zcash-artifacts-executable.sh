#!/usr/bin/env bash
#
# Make the zcash binaries downloaded into ./src executable on the test runners.
#
# The `zallet` launcher execs a sibling backend binary, so backend binaries are
# made executable too when present.
#
# Inputs (environment):
#   FILE_EXT  executable suffix, e.g. ".exe" (optional, default empty)

set -euo pipefail

ext="${FILE_EXT:-}"

chmod +x "./src/zebrad$ext"
chmod +x "./src/zainod$ext"
chmod +x "./src/zallet$ext"
chmod +x "./src/lightwalletd$ext"
for zallet_backend in zallet-zebra zallet-zaino; do
    if [ -f "./src/${zallet_backend}$ext" ]; then
        chmod +x "./src/${zallet_backend}$ext"
    fi
done
