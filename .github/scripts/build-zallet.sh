#!/usr/bin/env bash
#
# Build the Zallet binaries for the integration-tests CI, from the wallet
# checkout in the current directory.
#
# Zallet is structured as a thin `zallet` launcher (root workspace) that reads
# the config's top-level `backend` key and execs a matching per-backend binary
# (`zallet-zebra`, `zallet-zaino`), each built in its own workspace under
# backends/. The integration RPC suite runs the same tests against both
# backends, so we build the launcher plus BOTH `zallet-zaino` and
# `zallet-zebra`. Backend selection is structural (which workspace binary the
# launcher execs, driven by the config `backend` key the test framework writes
# into qa/defaults/zallet/zallet.toml), not a cargo feature, so neither backend
# build takes backend-selecting flags.
#
# The built binaries are copied into ./dist. When running under GitHub Actions,
# DB_DUMP_ROOT is appended to $GITHUB_ENV naming the workspace build directory
# that holds the db_dump vendored by zewif-zcashd (used by stage-zallet-db-dump.sh).
#
# Inputs (environment):
#   RUST_TARGET    cargo --target triple (required)
#   FILE_EXT       executable suffix, e.g. ".exe" (optional, default empty)
#   ZCASHD_IMPORT  extra cargo build args, e.g. "--features zcashd-import"; empty
#                  on mingw32, where its pqcrypto-mlkem dep fails to cross-compile
#                  to windows-gnu and the tests needing it are Linux-only

set -eo pipefail

target="${RUST_TARGET:?RUST_TARGET must be set}"
ext="${FILE_EXT:-}"
zcashd_import="${ZCASHD_IMPORT:-}"

dist=dist
mkdir -p "$dist"

# Launcher (root workspace) + both backend binaries (each its own workspace).
cargo build --release --target "$target" --bin zallet
cargo build --release --target "$target" \
    --manifest-path backends/zaino/Cargo.toml \
    $zcashd_import --bin zallet-zaino
cargo build --release --target "$target" \
    --manifest-path backends/zebra/Cargo.toml \
    $zcashd_import --bin zallet-zebra

cp "target/$target/release/zallet$ext" "$dist/"
cp "backends/zaino/target/$target/release/zallet-zaino$ext" "$dist/"
cp "backends/zebra/target/$target/release/zallet-zebra$ext" "$dist/"

if [ -n "${GITHUB_ENV:-}" ]; then
    echo "DB_DUMP_ROOT=backends/zaino/target/$target/release/build" >> "$GITHUB_ENV"
fi
