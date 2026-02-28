# Claude Code Handoff: Robots (backend-aligned)

## What Claude should treat as **truth**
- `robots/schemas/robot_run.schema.json` is the only payload contract.
- `sync_validate` must pass before any release gate is considered satisfied.

## How this syncs to other zips
Claude should **not** merge robots into Admin/Expo/Roadmap/TrustSpine repos.
Instead:
1) Unzip each repo to a workspace
2) Update `robots.config.yaml` paths
3) Run validate; then smoke robots as needed

## Backend alignment goal
Robots exist to give Claude *backend confidence* that:
- Admin portal is present and buildable
- Expo app is present and buildable
- Roadmap gates exist and are readable
- TrustSpine contract docs are present

Robots then emit **receipts-like evidence** (logs/artifacts) and an optional ingest POST.
