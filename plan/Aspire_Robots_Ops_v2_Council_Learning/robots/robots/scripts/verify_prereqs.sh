#!/usr/bin/env bash
set -euo pipefail
need(){ command -v "$1" >/dev/null 2>&1 || { echo "Missing required tool: $1" >&2; exit 1; }; }
need python3
need unzip

echo "OK: python3 + unzip present."

# Optional (only needed for smoke scenarios)
for opt in node npm npx docker curl; do
  if command -v "$opt" >/dev/null 2>&1; then
    echo "OK: optional tool present: $opt"
  else
    echo "WARN: optional tool missing: $opt"
  fi
done

if command -v k6 >/dev/null 2>&1; then
  echo "OK: optional tool present: k6"
else
  echo "WARN: optional tool missing: k6 (api_smoke will fall back to curl)"
fi
