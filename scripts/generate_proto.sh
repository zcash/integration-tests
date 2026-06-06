#!/usr/bin/env bash
# Regenerate Python gRPC stubs from the lightwallet-protocol subtree.
#
# Run this after updating the subtree to a new protocol version:
#
#   git subtree pull --prefix=lightwallet-protocol \
#     https://github.com/zcash/lightwallet-protocol.git <NEW_VERSION> --squash
#
# Then regenerate and commit the updated stubs:
#
#   scripts/generate_proto.sh
#   git add qa/rpc-tests/test_framework/proto/
#   git commit -m "update: regenerate gRPC stubs from lightwallet-protocol <NEW_VERSION>"
#
# Requirements: grpcio-tools (developer tool, not a runtime dependency)
#   uv tool install grpcio-tools
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROTO_SRC="$REPO_ROOT/lightwallet-protocol/walletrpc"
PROTO_OUT="$REPO_ROOT/qa/rpc-tests/test_framework/proto"

python-grpc-tools-protoc \
  -I "$PROTO_SRC" \
  --python_out="$PROTO_OUT" \
  --pyi_out="$PROTO_OUT" \
  --grpc_python_out="$PROTO_OUT" \
  "$PROTO_SRC/compact_formats.proto" \
  "$PROTO_SRC/service.proto"

# grpcio-tools generates flat imports that break when loaded as a package.
# Fix them to use relative imports in all generated Python artifacts.
for generated_file in \
  "$PROTO_OUT/service_pb2.py" \
  "$PROTO_OUT/service_pb2.pyi" \
  "$PROTO_OUT/service_pb2_grpc.py"
do
  sed -i 's/^import compact_formats_pb2 as/from . import compact_formats_pb2 as/' "$generated_file"
  sed -i 's/^import service_pb2 as/from . import service_pb2 as/' "$generated_file"
done

echo "Stubs written to $PROTO_OUT"
