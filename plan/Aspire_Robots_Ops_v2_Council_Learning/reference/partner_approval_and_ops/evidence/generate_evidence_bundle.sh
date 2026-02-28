#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./generate_evidence_bundle.sh <TRACE_ID> <OUTPUT_DIR>
# Requires psql env vars or connection string in DATABASE_URL.

TRACE_ID="${1:-}"
OUT_DIR="${2:-}"

if [[ -z "$TRACE_ID" || -z "$OUT_DIR" ]]; then
  echo "Usage: $0 <TRACE_ID> <OUTPUT_DIR>" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# Export receipts + provider calls filtered by trace_id.
# You may need to edit table names to match your canonical Trust Spine schema.

psql "${DATABASE_URL:-}" -v trace_id="$TRACE_ID" -f "$(dirname "$0")/export_receipts.sql" > "$OUT_DIR/receipts.json" || true
psql "${DATABASE_URL:-}" -v trace_id="$TRACE_ID" -f "$(dirname "$0")/export_provider_calls.sql" > "$OUT_DIR/provider_calls.json" || true

cp "$(dirname "$0")/replay_bundle_template.md" "$OUT_DIR/replay_bundle.md"

echo "Evidence bundle written to: $OUT_DIR"
