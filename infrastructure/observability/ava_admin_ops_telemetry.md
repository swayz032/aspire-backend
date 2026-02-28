# Ops Telemetry Facade API (LLM-safe)

**Source:** Ava Admin Enterprise Handoff v2

## Purpose
Expose near-real-time backend state to Admin Ava **without** direct DB/observability backend access.

## Data sources (read-only tools)
- incidents
- receipts ledger
- provider call logs
- traces (correlation_id)
- rollout/registry state
- outbox lag / stuck jobs
- golden signals

## Golden signals (minimum)
- **Approval latency** (proposal -> approval)
- **Outbox lag** (job queued -> started)
- **Execution success rate** (by provider + action)
- **Receipt emission rate** (proposal/approval/execution)
- **Provider error rate** + retry count
- **Poison jobs / dead-letter rate**

## Endpoints (internal)
- GET /admin/ops/health
- GET /admin/ops/incidents?state=open
- GET /admin/ops/receipts?correlation_id=...
- GET /admin/ops/provider-calls?provider=...&code=...&window=...
- GET /admin/ops/outbox?state=stuck&age_gt=...
- GET /admin/ops/rollouts?active=true

## Controls
- Tenant scope required (`suite_id`, `office_id`)
- Field-level redaction (PII, tokens, bank details)
- Rate limits + audit receipts for reads

## Contract
- OpenAPI: `plan/contracts/ava-admin/ops_telemetry_facade.openapi.yaml`

## Cross-reference
- Existing SLI/SLO: `infrastructure/observability/SLI_SLO.md`
- DLP redaction matrix: `plan/specs/ava-admin/dlp_redaction_matrix.md`
- Implementation target: Phase 2
