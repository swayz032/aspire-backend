# Integration (Robots zip ↔ other Aspire zips)

This robots package stays separate. It syncs with other projects by **pinning roots** in `robots.config.yaml` and validating **sentinel files**.

## Expected roots (default)
- Admin portal: `zenith-operations-suite-main/`
- Expo app: `Aspire-UI-Expokit/`
- Updated roadmap: `plan/`
- TrustSpine: zip root contains `ADR/`, `PROMPTS/`, etc.

If any zip changes its internal folder name/layout, update `robots.config.yaml` `paths.*` and/or `sentinels.*`.

## Backend ingest (optional)
If you deploy a backend ingest endpoint (e.g., Supabase Edge Function), set:
- `ingest.enabled: true`
- `ingest.url`
- `ingest.token`

Robots will POST the `RobotRun` payload.
