#!/usr/bin/env bash
#
# Stage the BDB 6.2 db_dump vendored by zewif-zcashd next to the zallet binaries
# in ./dist, from the wallet checkout in the current directory.
#
# zewif-zcashd's build.rs writes the db_dump path via cargo:rustc-env, which does
# not survive the build->test runner hop, and Ubuntu's PATH db_dump is BDB 5.3.
# Shipping it in the artifact lets zallet's which::which("db_dump") find the right
# version on the test runner.
#
# Inputs (environment):
#   DB_DUMP_ROOT  cargo build-output directory to search (required; set by
#                 build-zallet.sh)

set -euo pipefail

root="${DB_DUMP_ROOT:?DB_DUMP_ROOT must be set}"

db_dump="$(find "$root" -path '*/zewif-zcashd-*/out/db_dump' -type f -print -quit)"
if [ -z "$db_dump" ]; then
    echo "Vendored db_dump not found under $root/zewif-zcashd-*/out/" >&2
    exit 1
fi

cp "$db_dump" dist/db_dump
