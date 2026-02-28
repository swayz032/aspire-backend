# Scenarios

## sync_validate (default)
Checks for sentinel files in each pinned repo root.

## repo_health (smoke)
Runs configurable commands in Admin and Expo roots. Captures logs as evidence.

## api_smoke (smoke)
Hits `{base_url}{health_path}` and enforces basic latency and status checks. If `k6` is installed and enabled, runs a small k6 script.

## ui_smoke (smoke)
Runs Playwright smoke tests if Node/npm are available. Captures Playwright HTML report + screenshot evidence.
