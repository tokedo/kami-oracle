#!/usr/bin/env bash
# Vendor the kami-oracle client/ package into a downstream repo.
# Mirrors scripts/vendor-context.sh — the goal is to keep client
# distribution simple (drop a directory in, no PyPI publish step).
#
# Usage: bash scripts/vendor-client.sh /path/to/target-repo
#
# Result: writes to <target>/kami_oracle_client/ (the import path
# stays usable as `from kami_oracle_client import OracleClient` on the
# downstream side; on the oracle host it's `from client import ...`).

set -euo pipefail

TARGET="${1:?Usage: $0 /path/to/target-repo}"
SRC="$(cd "$(dirname "$0")/.." && pwd)/client"
DST="$TARGET/kami_oracle_client"

if [[ ! -d "$TARGET" ]]; then
  echo "ERROR: target '$TARGET' is not a directory." >&2
  exit 1
fi

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: source '$SRC' not found — run from the kami-oracle repo." >&2
  exit 1
fi

mkdir -p "$DST"
rm -rf "$DST"/*
# Copy everything except Python bytecode caches.
(cd "$SRC" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - .) | (cd "$DST" && tar -xf -)

# Record provenance so downstream pins a known revision.
ORACLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if git -C "$ORACLE_ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
  ORACLE_SHA="$(git -C "$ORACLE_ROOT" rev-parse --short HEAD)"
else
  ORACLE_SHA="unknown"
fi
echo "$ORACLE_SHA" > "$DST/ORACLE_SHA"

FILE_COUNT="$(find "$DST" -maxdepth 1 -type f | wc -l | tr -d ' ')"
echo "Vendored client/ ($FILE_COUNT files) from kami-oracle @ $ORACLE_SHA to $DST"
