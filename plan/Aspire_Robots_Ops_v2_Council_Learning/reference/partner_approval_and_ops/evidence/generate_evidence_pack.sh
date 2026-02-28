#!/usr/bin/env bash
set -euo pipefail

# Evidence pack generator
# Exports redacted receipts + provider_call_log samples and writes a replay bundle stub.
#
# Requirements:
# - psql installed
# - env vars set: DATABASE_URL, SUITE_ID, TRACE_ID (optional)

OUT_DIR="${1:-evidence_pack_out}"
mkdir -p "$OUT_DIR"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL is not set" >&2
  exit 1
fi

export PSQL="psql \"$DATABASE_URL\" -v ON_ERROR_STOP=1"

SUITE_ID="${SUITE_ID:-}"
if [[ -z "$SUITE_ID" ]]; then
  echo "ERROR: SUITE_ID is not set" >&2
  exit 1
fi

TRACE_ID="${TRACE_ID:-}"

echo "Exporting receipts..."
$PSQL -v suite_id="$SUITE_ID" -f "$(dirname "$0")/export_receipts.sql" > "$OUT_DIR/receipts.csv"

echo "Exporting provider call logs..."
$PSQL -v suite_id="$SUITE_ID" -f "$(dirname "$0")/export_provider_calls.sql" > "$OUT_DIR/provider_call_log.csv"

cp "$(dirname "$0")/replay_bundle_template.md" "$OUT_DIR/replay_bundle.md"

cat > "$OUT_DIR/README.md" <<README
# Evidence Pack Output

Generated: $(date -u)
Suite ID: $SUITE_ID
Trace ID: ${TRACE_ID:-N/A}

Files:
- receipts.csv
- provider_call_log.csv
- replay_bundle.md (fill in and attach to partner review)
README

echo "Done. Evidence pack at: $OUT_DIR"
