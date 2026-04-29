---
name: Office Memory Engine V1 — Pass 12 PRR Findings
description: SRE review findings for Memory Engine + Coordination Spine V1, Pass 12, 2026-04-28
type: project
---

## Review date: 2026-04-28

## Recommendation: SHIP-WITH-FOLLOWUPS (3 conditions, not blockers)

## What passed

- Gate 1 (Testing): All 16 test files present and passing. Frontend 4 routes + 14 components confirmed. e2e specs present.
- Gate 3 (Reliability): memory_service.py and transcript_event_refinery.py have correct try/except + dead-letter + re-raise patterns. Idempotency keys enforced via UNIQUE constraint + service-layer dedup. Fail-closed on unknown event_type, missing scope, cross-tenant mismatch.
- Gate 5 (Security, partial): All 9 spine tables have ENABLE + FORCE ROW LEVEL SECURITY. No ts-ignore suppressions in V1 frontend components. Tenant isolation pre-checked in MemoryService above DB layer.
- Gate 4 (Operations): Runbook authored at `Aspire-desktop/docs/runbooks/office-memory-engine.md` covering rollback table, dual-read disable, dead-letter replay, agent re-sync.

## What has conditional findings

### Law #2 Gap — brief_materializer receipts (HIGH)
`build_office_brief`, `build_finance_brief`, `build_thread_brief` all upsert to *_brief_cache tables without emitting receipts. These are state-changing operations. The materializer reads receipts as input data but does not produce them. Fix: call `store_receipts([brief_refresh_receipt])` after each successful upsert.

### Law #5 Gap — session broker capability token (HIGH)
`POST /v1/session-broker/start` returns `allowed_tools` as a plain static list — not a signed, short-lived capability token. No `X-Capability-Token` is minted or returned. The spec (routes/memory.py line ~511) says "simplified — full registry integration in Pass 6" but Pass 6 is complete. This violates Law #5. Fix: integrate `token_service.mint_token(scope, tools, ttl=60)` and return the token in `SessionStartResponse`.

### Law #9 Gap — DLP not called in write path (HIGH)
`title` and `summary` from user-generated content are written to `memory_objects` without calling `get_dlp_service().redact()`. The `readyz` probe confirms DLP is available. The gap is in the write path. Fix: call DLP on `envelope.title` + `envelope.summary` before the DB insert in `memory_service.py`.

### Gate 2 Gap — readyz does not probe spine (MEDIUM)
`/readyz` does not verify `threads`, `memory_objects`, or Temporal worker connectivity. If the memory spine is corrupted or Temporal is down, the service reports healthy. Fix: add a quick probe (e.g., `SELECT 1 FROM public.memory_objects LIMIT 1`) and Temporal connection check to `readyz`.

### Gate 2 Gap — SLO metrics missing (MEDIUM)
`services/metrics.py` and `services/slo_receipts.py` have no entries for: memory_write latency, brief_refresh lag, proactive_candidate creation rate, memory_event_inbox queue depth. Fix: register Prometheus counters/histograms for these four metrics in Pass 13.

### Gate 4 Gap — 24h staging soak (MEDIUM)
Plan §13 Pass 12 requires a 24h soak test on staging. No evidence this was run. The runbook covers rollback but does not include a staging soak reference. This is a process gap, not a code gap.

## Migration numbering offset (informational)
Plan spec called migrations 094-099. Actual files are 095-100. Migration 101 (deprecate_legacy_agent_memory, the cleanup cutover) is not present on disk and must NOT be run until 7-day parity confirmed. Verified: the plan's "migration 099 (cleanup)" is correctly deferred.
