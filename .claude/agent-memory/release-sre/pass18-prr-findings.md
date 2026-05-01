---
name: Pass 18 PRR Findings
description: Detailed evidence and gap analysis from the Pass 18 production readiness review of the Office Memory Engine + telephony extensions (Pass 16 modules)
type: project
---

# Pass 18 PRR Detailed Findings

Review date: 2026-04-29
Components reviewed:
- services/twilio_provisioning.py
- services/elevenlabs_phone.py
- services/sms_io.py
- services/ingestion/* (13 adapters)
- routes/telephony.py, sarah.py, front_desk.py, sms.py, ingestion.py
- tests/security/test_rls_*.py (7 files)
- infrastructure/supabase/migrations/100-103
- Aspire-desktop/docs/runbooks/
- Aspire-desktop/server/routes.ts
- Aspire-desktop/lib/api/officeMemory.ts

## Gate 1 — Testing

### What was NOT testable (no pytest access from Windows host)
pytest was not run; WSL was not available in this session. Test file counts and structure assessed by static inspection.

### RLS Evil Test files confirmed present (7 files)
- test_rls_memory_objects.py — MemoryService cross-tenant SELECT/INSERT/UPDATE
- test_rls_threads.py
- test_rls_proactive_candidates.py
- test_rls_tenant_phone_numbers.py (Pass 16 new)
- test_rls_front_desk_configs.py (Pass 16 new)
- test_rls_sms_messages.py (Pass 16 new)
- test_rls_memory_objects_ingestion.py (Pass 14 new)

### Ingestion adapter unit tests (11 files confirmed)
tests/services/ingestion/: sms, invoice, quote, call, elevenlabs, anam, zoom, contract, document, calendar, signatures

### MISSING tests for Pass 16 new services
- test_twilio_provisioning.py — does not exist
- test_elevenlabs_phone.py — does not exist
- test_sms_io.py — does not exist
- test_front_desk_routes.py — does not exist
- test_telephony_routes.py — does not exist
- test_sarah_personalization.py — does not exist

test_sarah_front_desk.py and test_sarah_skillpack.py exist but test the ElevenLabs skillpack-facing layer (Pass 4/5), not the new personalization webhook added in Pass 16.

### Coverage assessment
Legacy memory engine modules (Pass 1-13): well covered.
Pass 14 ingestion adapters: well covered (11 test files).
Pass 16 telephony/SMS/front-desk services: NOT covered (0 unit test files).

## Gate 2 — Observability

### Prometheus metrics: DEFINED, NOT instrumented on new code
- metrics.py defines REQUEST_COUNTER, RECEIPT_WRITE_COUNTER, TOKEN_MINT_COUNTER, TOOL_EXECUTION_COUNTER, etc.
- None of twilio_provisioning.py, elevenlabs_phone.py, sms_io.py, routes/sarah.py call METRICS.
- No `aspire_telephony_requests_total`, `aspire_sms_send_total`, `aspire_personalization_latency_seconds` exist.
- Existing receipt_write_counter WOULD fire if receipt_store.store_receipts is wired to emit metrics (unclear without tracing receipt_store impl).

### trace_id / correlation_id propagation
- memory.py routes: trace_id and correlation_id present (confirmed in memory_service route handlers).
- twilio_provisioning.py: no trace_id in receipts or logs.
- sms_io.py: no trace_id in receipts or logs.
- routes/sarah.py: no trace_id in receipts or logs.
- This means personalization webhook calls and SMS sends cannot be cross-correlated with the upstream agent invocation.

### Health endpoints
- /healthz, /livez, /readyz: all confirmed in server.py.
- /v1/ingest/healthz: confirmed at routes/ingestion.py:419.
- No specific health probe for telephony tables (but main /readyz covers DB connectivity).

### SLO definitions
- infrastructure/observability/SLI_SLO.md referenced in metrics.py but not inspected. File path exists per metrics.py:4.
- No explicit p95/p99 latency SLOs found for personalization (<800ms budget noted inline in sarah.py:23).

## Gate 3 — Reliability

### Circuit breakers: ABSENT on all new code
Grep result: zero occurrences of cockatiel/CircuitBreaker/tenacity/backoff in:
- twilio_provisioning.py
- elevenlabs_phone.py
- sms_io.py
- All ingestion adapters (checked for circuit_breaker pattern — not found)

### Retry with exponential backoff: ABSENT
No tenacity decorators or manual retry loops in any new Pass 16 service code.

### Timeout: COMPLIANT
_TIMEOUT_SECONDS = 4.5 hardcoded in twilio_provisioning.py:51, elevenlabs_phone.py:36, sms_io.py:40, front_desk.py:58.

### Idempotency: PARTIAL
- purchase_number: idempotency_key + in-memory dict cache (twilio_provisioning.py:57).
  RISK: in-memory = reset on pod restart = potential duplicate Twilio purchases in multi-replica deploys.
- EL import: 409 handling returns existing record (elevenlabs_phone.py:97). Good.
- send_sms: SHA256(thread_id + body + minute_bucket) idempotency_key passed through (sms_io.py:69-77).
  The key is passed to Twilio as an X-Idempotency-Key? — Not found. The key is stored in sms_messages.idempotency_key but is NOT sent to Twilio Messages API as an idempotency key header. This means duplicate Twilio sends are possible.
- update_sms_status: message_sid lookup is effectively idempotent (status overwrite on same sid).

### Dependency failure behavior
- Twilio down: raises TwilioProvisioningError / SmsIoError, cuts failure receipt, propagates as 502.
- EL down: raises ElevenLabsPhoneError, caught in purchase_number rollback, cuts failure receipt.
- Supabase down: raises SupabaseClientError, propagates as 500. No fallback.
- Redis: Not a dependency for new modules (idempotency is in-memory, not Redis).
- In all cases: fail closed (no silent degradation). Law #3 satisfied.

## Gate 4 — Operations

### Runbooks present
- office-memory-engine.md: EXISTS. Comprehensive. Covers migrations 100-101 rollback, shadow mode, dead-letter replay, EL sync, Anam sync, 7-day parity schedule.
- telephony.md: MISSING
- sarah-personalization.md: MISSING
- sms.md: MISSING
- ingestion-adapters.md: MISSING
- postmortem-template.md: MISSING

### Migration rollback documentation
- Migration 100 (episode→memory data migration): rollback in runbook (set status='superseded' on backfilled rows).
- Migration 101 (cleanup): blocked by 7-day shadow mode window. Runbook covers.
- Migration 102 (telephony tables): CREATE TABLE IF NOT EXISTS — reversible by DROP TABLE. No explicit rollback SQL documented. Tables are additive only (no existing data touched). Service-layer rollback: remove router includes from server.py.
- Migration 103 (memory_type constraint expansion): reversible by re-applying prior CHECK constraint. Non-destructive. Documented as "non-destructive, idempotent, reversible" in migration header.

### 24h soak test
Not run. This is expected and must be a gate condition.

### Deployment procedure
No formal step-by-step deploy doc for Pass 16 specifically. The runbook covers rollback but not the forward deploy procedure. Railway deployment is via `railway up` based on existing patterns.

## Gate 5 — Security

Deferred to parallel security-reviewer audit (task a376ae19806b0ca95).
From code inspection:
- Law #9 enforced: twilio_account_sid/auth_token never logged (twilio_provisioning.py:99-105).
- EL API key never logged (elevenlabs_phone.py:44-51), only 8-char prefix in debug logs.
- caller_id truncated to first 6 digits in sarah.py:210.
- SMS body truncated to 80 chars in sms_io.py:237.
- Capability token validated server-side on all Yellow-tier routes.
- HMAC signature verification on all webhook routes (sarah.py, ingestion.py).

## Outstanding Items (flagged for ship-gate conditions)

1. EL transfer-rule sync: run `node scripts/sync-elevenlabs-transfer-rules.mjs` (pre-ship MUST).
2. Server-side proxy routes for new APIs (twilio/front-desk/sms/sarah) — not in server/routes.ts.
3. Backend GET /v1/office-memory/{memoryId} route does not exist.
4. officeMemory.ts 401 retry-once via supabase.auth.refreshSession() not implemented.
5. Forest photo asset for FrontDeskSetupHero.tsx.
6. 24h soak test (post-deploy, before calling Pass 18 closed).
