# Aspire Robots (Standalone + Syncable) — v3.1

This is a **robots-only** toolkit that stays separate from your other Aspire zips, but **syncs** with them via a single `robots.config.yaml` that pins each repo root path.

It supports three layers:
1) **Sync validation** (fast, deterministic): detect structural drift across Admin / Expo / Roadmap / TrustSpine.
2) **Smoke mode** (real checks): repo health commands, API health smoke, and UI journey smoke.
3) **Reporting**: emits a single `RobotRun` JSON that can be posted to a backend ingest endpoint (optional).

## Quick start (bootstrap from your zips)
```bash
bash robots/scripts/bootstrap_workspace_from_zips.sh \
  --admin "./Aspire-admin-portal.zip" \
  --expo "./Aspire-UI-Expokit-Minimal.zip" \
  --roadmap "./Aspire updated roadmap.zip" \
  --trust "./Trust spine final handoff .zip"

# Validate sync
bash robots/scripts/run_sync_suite.sh robots.config.yaml
```

## Smoke mode
Edit `robots.config.yaml`:
- set `mode: smoke`
- add scenarios in `scenarios.enabled` (e.g. `repo_health`, `api_smoke`, `ui_smoke`)

Then:
```bash
bash robots/scripts/verify_prereqs.sh
bash robots/scripts/setup_venv.sh
python3 robots/run_suite.py --config robots.config.yaml --env staging --version-ref local
```

Outputs:
- `robots/out/manifest.json`
- `robots/out/results/<run_id>.json`
- evidence under `robots/out/evidence/<run_id>/`

Claude handoff notes:
- `claude/CLAUDE_HANDOFF_ROBOTS.md`
- `claude/CHECKLIST.md`
