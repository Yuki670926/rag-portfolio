#!/usr/bin/env bash
# Lambda Layer build script
#
# Builds dependencies in the official Lambda image (Amazon Linux / Python 3.12)
# so native binaries (.so) are Lambda-compatible, avoiding the Windows .pyd
# problem that occurs when pip installing on a Windows host.
#
# Output goes to layers/<name>/build/python so that the zip created by
# Terraform (source_dir = layers/<name>/build) has the required python/ top
# level directory. The build/ dir is gitignored (build artifacts not committed).
#
# NOTE: the clean step runs inside Docker too. Files created by the previous
# build are owned by the container's root user, so removing them from a
# Windows/WSL host directly fails with "Permission denied". Cleaning inside the
# same image avoids that ownership mismatch.
#
# Usage:
#   ./build-layer.sh <layer-name>
#   e.g. ./build-layer.sh authorizer
#        ./build-layer.sh ingest-query
#
# Requirements:
#   - Docker running
#   - layers/<layer-name>/requirements.txt exists

set -euo pipefail

LAYER_NAME="${1:?Usage: ./build-layer.sh <layer-name>}"
LAYER_DIR="layers/${LAYER_NAME}"
BUILD_DIR="${LAYER_DIR}/build/python"
PYTHON_RUNTIME="public.ecr.aws/lambda/python:3.12"

if [ ! -f "${LAYER_DIR}/requirements.txt" ]; then
  echo "ERROR: ${LAYER_DIR}/requirements.txt not found" >&2
  exit 1
fi

echo "==> Building layer '${LAYER_NAME}' (${PYTHON_RUNTIME})"

# Clean previous build inside Docker (root) to avoid host-side permission errors,
# then pip install. -t build/python keeps the python/ layout Lambda expects.
docker run --rm \
  --entrypoint /bin/bash \
  -v "$(pwd)/${LAYER_DIR}:/var/task" \
  "${PYTHON_RUNTIME}" \
  -c "rm -rf /var/task/build && \
      pip install -r /var/task/requirements.txt -t /var/task/build/python --no-cache-dir && \
      find /var/task/build/python -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
      true"

echo "==> Done: ${BUILD_DIR}"
echo "    Linux native binaries (.so), if any:"
find "${BUILD_DIR}" -name "*.so" | head -5 || echo "    (none = pure Python only)"
echo "    Windows binaries (.pyd) - should be ZERO:"
find "${BUILD_DIR}" -name "*.pyd" | wc -l
