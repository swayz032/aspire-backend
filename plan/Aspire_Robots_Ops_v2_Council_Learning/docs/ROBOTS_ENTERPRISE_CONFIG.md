# Robots enterprise config (what to change)

## Problem
Robots sentinel paths often mismatch when Trust Spine folder layout evolves.

## Fix
Create a second config file (leave original intact):
- `robots/config/robots.config.enterprise.yaml`

Set sentinel paths to match the canonical core pack layout:
- Core lives at: `../Aspire_Core_Platform_v1/platform/` (relative when unpacked together)
- Trust spine docs live under: `platform/Aspire_Handoff_v1/trust-spine/...`

## Ingest contract
Robots should POST RobotRun results to:
- POST /robots/ingest

Backend should translate robot failures into:
- incident.opened receipt (trace_id attached)
- A2A triage message to incident channel
