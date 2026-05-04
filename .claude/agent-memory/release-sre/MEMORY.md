# Release SRE Agent Memory — Aspire Platform

## Pass 18 PRR (Office Memory Engine + Coordination Spine V1, Pass 16 telephony extensions)

- See `pass18-prr-findings.md` for detailed findings and patterns discovered during this review.
- Result: SHIP-WITH-CONDITIONS (6 conditions, 1 critical prerequisite)
- Review date: 2026-04-29

## Key Operational Patterns Discovered

### Idempotency Store (in-memory, not Redis)
- `twilio_provisioning.py` uses a Python dict (`_idem_store`) for idempotency — not Redis/Supabase.
- Resets on pod restart. Cross-pod duplicate purchases are possible in Railway multi-replica deploy.
- File: `backend/orchestrator/src/aspire_orchestrator/services/twilio_provisioning.py:57`

### Circuit Breakers: ABSENT on telephony + SMS paths
- `twilio_provisioning.py`, `elevenlabs_phone.py`, `sms_io.py` — zero circuit breaker patterns.
- Raw httpx calls, no tenacity/backoff retry wrappers.
- This is the single most dangerous reliability gap for the new telephony subsystem.

### Timeout Budget: COMPLIANT
- All new HTTP-calling code uses `_TIMEOUT_SECONDS = 4.5` (< 5s Law #10 standard).
- Both `twilio_provisioning.py:51` and `elevenlabs_phone.py:36` and `sms_io.py:40`.

### Prometheus Metrics: NOT wired on new modules
- `metrics.py` defines counters (receipts, requests, etc.) but telephony/SMS/sarah routes do NOT call METRICS.
- Missing: `aspire_telephony_requests_total`, `aspire_sms_send_total`, `aspire_personalization_latency`.
- Existing metrics file: `backend/orchestrator/src/aspire_orchestrator/services/metrics.py`

### Receipt Coverage: EXCELLENT
- All state-changing operations cut receipts: purchase_number, release_number, send_sms, personalization, front_desk_config_save, routing_contact_*.
- Even denials and rollbacks cut failure receipts. Law #2 well-enforced.

### RLS Evil Tests: 7 files present, not run (no pytest access from Windows)
- `tests/security/test_rls_memory_objects.py`, `test_rls_threads.py`, `test_rls_proactive_candidates.py`
- `test_rls_tenant_phone_numbers.py`, `test_rls_front_desk_configs.py`, `test_rls_sms_messages.py`
- `test_rls_memory_objects_ingestion.py`
- Tests use service-layer mock approach (mocking supabase calls) — not live DB tests.

### Unit Tests: MISSING for new telephony/SMS services
- No `test_twilio_provisioning.py`, `test_elevenlabs_phone.py`, `test_sms_io.py` found.
- No `test_front_desk_routes.py`. `test_sarah_front_desk.py` exists but is for old skillpack path.
- Ingestion adapter tests: 11 files under `tests/services/ingestion/` — well covered.

### Runbooks: PARTIAL (1 of 4 required exists)
- `office-memory-engine.md` — EXISTS, comprehensive (migrations 100/101 rollback, shadow mode, replay, EL sync)
- `telephony.md` — MISSING
- `sarah-personalization.md` — MISSING
- `sms.md` — MISSING
- `postmortem-template.md` — MISSING

### Proxy Routes: NOT wired for new APIs
- `server/routes.ts` has `enrich-product` proxy at line 7918 as the template.
- No proxy routes for `/api/v1/twilio/*`, `/api/v1/front-desk/*`, `/api/v1/sms/*`, `/api/v1/sarah/personalization`.
- Frontend `officeMemory.ts` references same-origin `/api/v1/...` pattern but docs note this is planned.

### Backend GET-by-ID: MISSING
- `routes/memory.py` has `POST /v1/memory/search` but no `GET /v1/office-memory/{memoryId}`.
- `GET /v1/briefs/office/{id}`, `GET /v1/briefs/finance/{id}`, `GET /v1/briefs/thread/{id}` exist.

### EL Transfer Rules: UNFIXED (pre-ship prerequisite)
- Script exists: `Aspire-desktop/scripts/sync-elevenlabs-transfer-rules.mjs`
- Must run `railway login && node scripts/sync-elevenlabs-transfer-rules.mjs` before ship.
- Clears "at least one transfer rule required" error on all 6 EL agents.

### Migration Rollback Documentation
- Migrations 102/103: embedded rollback SQL in the SQL files themselves (verified).
- Migration 101: runbook documents `ASPIRE_MEMORY_DUAL_READ_ENABLED=true` re-enable path.
- All migrations use `CREATE TABLE IF NOT EXISTS` / idempotent DDL.

### trace_id / correlation_id Propagation: PARTIAL
- Memory service routes propagate `trace_id` and `correlation_id` (confirmed in memory.py).
- `twilio_provisioning.py`, `sms_io.py`, `sarah.py` do NOT attach trace_id to logs or receipts.
- This is a Law #2/observability gap, not an automatic blocker, but makes incident tracing harder.

### Health Endpoints
- `/healthz`, `/livez`, `/readyz` all exist in `server.py`.
- `/v1/ingest/healthz` exists in `routes/ingestion.py:419`.
- New telephony/SMS routes do not have dedicated health probes (acceptable; covered by main /healthz).

### 24h Soak Test
- Not run (as expected per PRR spec). Must be gated condition before calling Pass 18 closed.

## Per-Tenant Trust Hub + CNAM PRR (W1–W11, 2026-05-04)

- See `trust-hub-prr-findings.md` for detailed findings.
- Result: SHIP-WITH-CONDITIONS (6 conditions — C1 ASPIRE_REDIS_URL is the critical silent-failure blocker)
- Verdict artifact: `docs/proof-artifacts/per-tenant-trust-hub-SHIP-VERDICT.md`

### Trust Hub Key Patterns

- Migrations 109–120 span W1–W9. Migration 113 is security hardening (critical). Migration 114 is immutability trigger.
- `twilio_trust_hub.py` correctly wraps ALL Twilio calls with `resilient_call` + `TWILIO_RETRY` + `twilio_breaker()`. This is the RIGHT pattern. Contrast with Pass 18 (twilio_provisioning.py had zero circuit breakers).
- `trust_receipts.py` enforces PII guardrails via `_FORBIDDEN_PII_KEYS` frozenset — raises TrustReceiptError if any forbidden key appears.
- `test_trust_evil.py` is ABSENT — repeated gap across both Pass 18 and Trust Hub PRR.
- Trust-specific Prometheus metrics (`aspire_trust_onboarding_state_transitions_total`, 6 others) are ABSENT from `metrics.py` despite being in the plan.
- `_enqueue_advance_trust_state` in cron_jobs.py catches Redis connection failures silently (logger.warning, returns False). If ASPIRE_REDIS_URL is not configured, every KYB submit silently drops the ARQ job.
- Circuit breaker `_TWILIO_BREAKER` is per-process singleton — correct for single pod, becomes inconsistent state at multi-pod.
- RLS on trust tables uses `FOR SELECT` for authenticated (migration 113 hardening); all writes are service_role only.
- W7 A2P state machine does NOT forward capability_token_id to receipts — Law #5 audit gap.
- Runbooks for trust hub: 3 required files absent (`trust-hub-onboarding.md`, `number-swap.md`, `kyb-rejection-handling.md`).
