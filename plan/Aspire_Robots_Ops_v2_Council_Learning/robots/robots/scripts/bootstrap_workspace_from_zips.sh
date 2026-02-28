#!/usr/bin/env bash
set -euo pipefail

ADMIN=""
EXPO=""
ROADMAP=""
TRUST=""
OUTDIR="workspace"

usage(){
  echo "Usage: $0 --admin <Aspire-admin-portal.zip> --expo <Aspire-UI-Expokit-Minimal.zip> --roadmap <Aspire updated roadmap.zip> --trust <Trust spine final handoff .zip> [--outdir <workspace>]" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin) ADMIN="$2"; shift 2;;
    --expo) EXPO="$2"; shift 2;;
    --roadmap) ROADMAP="$2"; shift 2;;
    --trust) TRUST="$2"; shift 2;;
    --outdir) OUTDIR="$2"; shift 2;;
    -h|--help) usage;;
    *) echo "Unknown arg: $1" >&2; usage;;
  esac
done

[[ -n "$ADMIN" && -n "$EXPO" && -n "$ROADMAP" && -n "$TRUST" ]] || usage

for f in "$ADMIN" "$EXPO" "$ROADMAP" "$TRUST"; do
  [[ -f "$f" ]] || { echo "Missing zip: $f" >&2; exit 2; }
done

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR/admin" "$OUTDIR/expo" "$OUTDIR/roadmap" "$OUTDIR/trustspine"

unzip -q "$ADMIN" -d "$OUTDIR/admin"
unzip -q "$EXPO" -d "$OUTDIR/expo"
unzip -q "$ROADMAP" -d "$OUTDIR/roadmap"
unzip -q "$TRUST" -d "$OUTDIR/trustspine"

# Write robots.config.yaml (paths pinned to known top-level folders)
cat > robots.config.yaml <<CFG
mode: validate

env_defaults:
  staging:
    base_url: "http://localhost:5173"
  canary:
    base_url: "http://localhost:5173"
  production:
    base_url: "http://localhost:5173"

paths:
  admin_portal_root: "./$OUTDIR/admin/zenith-operations-suite-main"
  expo_app_root: "./$OUTDIR/expo/Aspire-UI-Expokit"
  roadmap_root: "./$OUTDIR/roadmap/plan"
  trustspine_root: "./$OUTDIR/trustspine"

sentinels:
  admin_portal:
    - "package.json"
    - "src/App.tsx"
  expo_app:
    - "package.json"
    - "app.json"
  roadmap:
    - "Aspire-Production-Roadmap.md"
    - "gates/gate-00-scope-lock.md"
  trustspine:
    - "ADR/ADR-0001_Canonical_Identity_Model.md"
    - "PROMPTS/TRUSTSPINE_ONBOARDING_MASTER_PROMPT.md"

scenarios:
  enabled:
    - sync_validate

ingest:
  enabled: false
  url: ""
  token: ""
CFG

echo "Workspace bootstrapped to ./$OUTDIR and robots.config.yaml written."
