# Learning Loop Specification

**Source:** Ava Admin Enterprise Handoff v2

## Prevention Pipeline

For Sev1/Sec events:
1. Postmortem draft generated (timeline from receipts)
2. New eval case added (repro + expected deny/allow)
3. New robot scenario added (synthetic reproduction)
4. Runbook updated (operator checklist + engineer details)
5. Promotion gate requires: eval_run_id + robot_run_id + approval receipt

## Robots Integration

Robots package stays separate. It syncs with other projects by pinning roots in `robots.config.yaml` and validating sentinel files.

### Backend ingest (optional)
If you deploy a backend ingest endpoint (e.g., Supabase Edge Function), set:
- `ingest.enabled: true`
- `ingest.url`
- `ingest.token`

Robots will POST the `RobotRun` payload.

### Admin Ava responsibilities
- Treat robot failures as first-class incidents:
  - create incident.opened receipt
  - attach RobotRun payload as evidence
- Convert recurring failures into:
  - new regression scenario
  - new eval case
  - runbook update
- Block promotion unless required robot/eval receipts exist.

## Cross-reference
- IncidentPacket schema: `plan/contracts/ava-admin/incident_packet.schema.json`
- OpsExceptionCard schema: `plan/contracts/ava-admin/ops_exception_card.schema.json`
- Implementation target: Phase 3+ (Certification)
