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
# Reproducibility: pip runs with --no-compile (no .pyc, which embed source
# mtimes and break byte-reproducibility) and every build file is touched to a
# fixed mtime. Both run as ROOT INSIDE the container, so there is no host-side
# ownership/permission problem (files created by the container are root-owned,
# so a host-side touch would fail with "Permission denied").
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
# All steps run inside Docker (root) for Lambda-compatible binaries AND to avoid
# host-side ownership errors when normalizing the build:
#   1. clean previous build
#   2. pip install with --no-compile (no .pyc -> reproducible)
#   3. defensively drop any __pycache__
#   4. pin every file's mtime so archive_file produces a stable hash
docker run --rm \
  --entrypoint /bin/bash \
  -v "$(pwd)/${LAYER_DIR}:/var/task" \
  "${PYTHON_RUNTIME}" \
  -c "rm -rf /var/task/build && \
      pip install -r /var/task/requirements.txt -t /var/task/build/python --no-cache-dir --no-compile && \
      find /var/task/build/python -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
      find /var/task/build -exec touch -t 200001010000 {} +; \
      true"
echo "==> Done: ${BUILD_DIR}"
echo "    Linux native binaries (.so), if any:"
find "${BUILD_DIR}" -name "*.so" | head -5 || echo "    (none = pure Python only)"
echo "    Windows binaries (.pyd) - should be ZERO:"
find "${BUILD_DIR}" -name "*.pyd" | wc -l
