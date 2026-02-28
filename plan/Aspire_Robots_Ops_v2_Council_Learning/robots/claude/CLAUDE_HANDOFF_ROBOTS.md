# Claude Code Handoff — Robots (Backend-aligned)

## Purpose
These robots are the **backend/operator verification layer** Claude should use to confirm:
- Admin portal, Expo app, roadmap, and TrustSpine handoff stay structurally aligned
- (optional) smoke checks pass before promoting releases

## Where robots fit
- **Validate mode**: fast structural gate (use on every change)
- **Smoke mode**: real checks (run before any release / after major wiring changes)

## Sync contract
Robots do not bundle the other repos. They link via `robots.config.yaml`:
- `paths.admin_portal_root`
- `paths.expo_app_root`
- `paths.roadmap_root`
- `paths.trustspine_root`

If Claude changes any top-level folder names, update config + sentinels.

## Outputs Claude should treat as authoritative
- `robots/out/results/<run_id>.json` (RobotRun payload)
- `robots/out/evidence/<run_id>/` (logs/screenshots)

## Optional ingest
If you deploy an ingest endpoint, set `ingest.enabled: true` and provide URL/token.
Robots will POST the RobotRun payload for the Admin portal to show "Proof of Success" using real data.
