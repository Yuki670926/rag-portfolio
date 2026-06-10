#!/usr/bin/env bash
# Lambda Layer build script
#
# Builds dependencies in the official Lambda image (Amazon Linux / Python 3.13)
# so native binaries (.so) are Lambda-compatible, avoiding the Windows .pyd
# problem that occurs when pip installing on a Windows host.
#
# Output goes to layers/<name>/build/python so that the zip created by
# Terraform (source_dir = layers/<name>/build) has the required python/ top
# level directory. The build/ dir is gitignored (build artifacts not committed).
#
# Reproducibility: builds install from requirements.lock (a fully-pinned closure
# incl. ALL transitive deps) with --no-deps, so the layer is byte-reproducible
# and Terraform's source_code_hash stays stable across rebuilds. requirements.txt
# is the human "intent"; requirements.lock is the resolved set actually installed.
# pip also runs --no-compile (no .pyc, which embed source mtimes) and every build
# file is touched to a fixed mtime. All run as ROOT INSIDE the container, so there
# is no host-side ownership problem (container-created files are root-owned).
#
# Regenerate the lock after editing requirements.txt (Docker required):
#   docker run --rm --entrypoint /bin/bash -v "<repo>/layers/<name>:/var/task" \
#     public.ecr.aws/lambda/python:3.13 \
#     -c "pip install -r /var/task/requirements.txt -t /tmp/b --no-cache-dir --no-compile -q && pip list --path /tmp/b --format=freeze | sort" \
#     > layers/<name>/requirements.lock
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
PYTHON_RUNTIME="public.ecr.aws/lambda/python:3.13"
if [ ! -f "${LAYER_DIR}/requirements.lock" ]; then
  echo "ERROR: ${LAYER_DIR}/requirements.lock not found (regenerate from requirements.txt; see header)" >&2
  exit 1
fi
echo "==> Building layer '${LAYER_NAME}' (${PYTHON_RUNTIME})"
# All steps run inside Docker (root) for Lambda-compatible binaries AND to avoid
# host-side ownership errors when normalizing the build:
#   1. clean previous build
#   2. pip install from requirements.lock with --no-deps --no-compile (fully pinned, reproducible)
#   3. defensively drop any __pycache__
#   4. pin every file's & dir's mtime so archive_file produces a stable hash
# NOTE: the slim Amazon Linux 2023 `python:3.12` image has NO `find`/`xargs`,
#       so we use bash globstar (**) + touch instead. Using find here silently
#       no-ops the mtime fix and reintroduces hash churn on every build.
docker run --rm \
  --entrypoint /bin/bash \
  -v "$(pwd)/${LAYER_DIR}:/var/task" \
  "${PYTHON_RUNTIME}" \
  -c "rm -rf /var/task/build && \
      pip install -r /var/task/requirements.lock -t /var/task/build/python --no-cache-dir --no-compile --no-deps && \
      shopt -s globstar dotglob && \
      rm -rf /var/task/build/**/__pycache__ 2>/dev/null; \
      touch -t 200001010000 /var/task/build /var/task/build/** ; \
      true"
echo "==> Done: ${BUILD_DIR}"
echo "    Linux native binaries (.so), if any:"
find "${BUILD_DIR}" -name "*.so" | head -5 || echo "    (none = pure Python only)"
echo "    Windows binaries (.pyd) - should be ZERO:"
find "${BUILD_DIR}" -name "*.pyd" | wc -l
