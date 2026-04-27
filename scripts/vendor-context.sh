#!/usr/bin/env bash
# Vendor required files from kamigotchi-context into kami_context/.
# Run this on first setup and whenever upstream ships new systems.
#
# Usage: bash scripts/vendor-context.sh /path/to/kamigotchi-context

set -euo pipefail

SRC="${1:?Usage: $0 /path/to/kamigotchi-context}"
DST="$(cd "$(dirname "$0")/.." && pwd)/kami_context"

if [[ ! -d "$SRC/integration" ]]; then
  echo "ERROR: $SRC/integration not found. Pass the kamigotchi-context repo root." >&2
  exit 1
fi

mkdir -p "$DST/abi"
rm -rf "$DST/abi"/*
cp -R "$SRC/integration/abi/"* "$DST/abi/"
cp "$SRC/integration/system-ids.md" "$DST/system-ids.md"
cp "$SRC/integration/chain.md"      "$DST/chain.md"

# Catalogs (Session 11+): skill effects + equipment effects derived
# from these CSVs to populate kami_static modifier columns.
mkdir -p "$DST/catalogs"
rm -rf "$DST/catalogs"/*
cp "$SRC/catalogs/skills.csv" "$DST/catalogs/skills.csv"
cp "$SRC/catalogs/items.csv"  "$DST/catalogs/items.csv"

# Record upstream version for reproducibility.
if git -C "$SRC" rev-parse --short HEAD >/dev/null 2>&1; then
  UPSTREAM_SHA="$(git -C "$SRC" rev-parse --short HEAD)"
else
  UPSTREAM_SHA="unknown"
fi
echo "$UPSTREAM_SHA" > "$DST/UPSTREAM_SHA"

ABI_COUNT="$(ls -1 "$DST/abi" | wc -l | tr -d ' ')"
echo "Vendored $ABI_COUNT ABIs from kamigotchi-context @ $UPSTREAM_SHA to $DST"
