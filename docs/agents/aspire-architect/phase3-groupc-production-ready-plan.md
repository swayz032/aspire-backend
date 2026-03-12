# Phase 3 Group C: Desktop Production Ready — Implementation Plan

**Date:** 2026-02-16
**Architect:** Aspire Architect Agent
**Scope:** Desktop production deployment + 5 Production Gates compliance
**Prior Work:** Phase 3 W9 complete (1847 backend tests, 78 Playwright tests, 40 verification tests)
**Target:** All 5 Production Gates PASS → production deployment ready

---

## 1) Scope & Assumptions

### What
Phase 3 Group C brings Aspire Desktop to full production readiness by closing all outstanding production gate gaps. This includes:
- Admin portal deployment (Railway service + GoDaddy subdomain)
- Anam avatar integration (Ava's visual presence in Desktop)
- Load testing against real services (Twilio, Stripe, ElevenLabs, LiveKit)
- Grafana dashboard deployment (latency, error rates, receipt throughput)
- Evil test execution + gap analysis
- Production soak plan execution (24h stability test)
- Carry-forward debt closure (4 conditions from prior phases)

### Why
Group B delivered core wiring with strong governance patterns (Ops Telemetry 100% receipt coverage, RLS enforcement, fail-closed auth). However, the release-sre verdict was **SHIP WITH CONDITIONS** due to:
- Zero load test execution (SLO compliance unknown)
- No orchestrator availability health check (Desktop → Orchestrator proxy fails open)
- Grafana dashboards exist but not deployed
- Admin portal Dockerfile exists but not deployed
- Anam avatar integration stubbed (placeholder iframe)

Group C closes these gaps to achieve **unconditional production readiness**.

### Boundary

**IN SCOPE:**
- Admin portal Railway deploy + GoDaddy DNS (admin.aspireos.app)
- Anam SDK integration in AvaDeskPanel (Cara avatar + Emma voice)
- Load test execution (orchestrator → real provider sandboxes)
- Grafana dashboard deployment (Railway metrics → Grafana dashboards)
- Evil test gap analysis + missing attack vectors
- Production soak plan (24h, error rate targets)
- Carry-forward debt closure (4 conditions from Phase 2.5/Group B)
- Incident runbook drill (simulate P0/P1 scenarios)

**OUT OF SCOPE:**
- Mobile app (ON HOLD per user directive)
- New feature development (no new skill packs, no new providers)
- Phase 0 debt (booking/service/profile CRUD receipts — deferred to Phase 4)
- Brain Layer receipts (LLM governance wrapper — deferred to Phase 4)
- Presence nonces (RED tier nonce binding — deferred to Phase 4)

### Assumptions

**A1:** Railway account has admin.aspireos.app subdomain available (user must configure DNS)
**A2:** User provides TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, ELEVENLABS_API_KEY, ANAM_API_KEY env vars
**A3:** Anam persona ID for Cara avatar is available in Anam lab (EXPO_PUBLIC_ANAM_PERSONA_ID)
**A4:** Twilio sandbox phone number is active for SMS/voice testing
**A5:** Stripe test mode API keys are configured (already present in Phase 2)
**A6:** ElevenLabs API quota sufficient for load test (voice synthesis calls)
**A7:** LiveKit cloud account active with room creation enabled
**A8:** Grafana accessible on localhost:3000 (existing setup from Phase 1)
**A9:** Prometheus scraping orchestrator metrics (existing setup from Phase 1)
**A10:** Load test runs against Railway-deployed orchestrator (NOT localhost)

### Dependencies

**External Systems:**
- Railway (deployment platform, metrics API)
- GoDaddy (DNS for admin.aspireos.app)
- Anam (avatar streaming service)
- Twilio (SMS/voice sandbox)
- Stripe (test mode)
- ElevenLabs (voice synthesis API)
- LiveKit (video conferencing cloud)
- Supabase (production database, RLS enforcement)

**Prior Work:**
- Phase 3 W9 complete (enterprise telephony + outbox worker)
- Phase 3 Group B complete (desktop wiring, Ops Telemetry, E2E tests)
- Phase 2.5 complete (ops receipts, kill switch, council, learning loop)
- Phase 1 complete (orchestrator core, receipts, capability tokens)

---

## 2) Repo/File Map

### Wave 1: Admin Portal Deploy (Railway + DNS)

**[CREATE] `Aspire-admin-portal/railway.json`** — Railway config for admin portal service
**[MODIFY] `Aspire-desktop/server/admin.py`** — Remove hardcoded localhost, use Railway URL from env
**[CREATE] `docs/operations/admin-portal-deploy.md`** — Deployment checklist + rollback procedure
**[CREATE] `backend/tests/e2e/playwright/admin-portal-health.spec.ts`** — Smoke test for deployed admin portal

### Wave 2: Anam Avatar Integration

**[MODIFY] `Aspire-desktop/lib/anam.ts`** — Add Anam SDK client init, session management
**[MODIFY] `Aspire-desktop/components/desktop/AvaDeskPanel.tsx`** — Replace iframe with Anam SDK components
**[MODIFY] `Aspire-desktop/package.json`** — Add Anam SDK dependency
**[CREATE] `Aspire-desktop/hooks/useAnamAvatar.ts`** — React hook for Anam session lifecycle
**[CREATE] `backend/tests/e2e/playwright/ava-anam-avatar.spec.ts`** — Verify avatar renders on "Video with Ava" toggle

### Wave 3: Load Test Execution

**[MODIFY] `backend/orchestrator/scripts/load_test.py`** — Add real provider integration flags
**[CREATE] `backend/orchestrator/scripts/load_test_real_services.py`** — Load test harness hitting Twilio, Stripe, ElevenLabs, LiveKit
**[CREATE] `docs/operations/load-test-real-services-report.md`** — Results from live load test
**[CREATE] `backend/orchestrator/tests/test_load_test_integration.py`** — Unit tests for load test harness helpers

### Wave 4: Grafana Dashboard Deployment

**[CREATE] `infrastructure/grafana/dashboards/aspire-desktop.json`** — Desktop latency, error rate, request count
**[CREATE] `infrastructure/grafana/dashboards/aspire-orchestrator.json`** — Orchestrator p50/p95/p99, tool success rate
**[CREATE] `infrastructure/grafana/dashboards/aspire-receipts.json`** — Receipt throughput, chain integrity, RLS violations
**[MODIFY] `infrastructure/grafana/provisioning/dashboards.yml`** — Auto-provision dashboards on Grafana startup
**[CREATE] `docs/operations/grafana-setup.md`** — Grafana deployment + dashboard import procedure

### Wave 5: Evil Test Gap Analysis + Execution

**[MODIFY] `backend/orchestrator/tests/test_evil_security.py`** — Add missing attack vectors (see Risk Register R2)
**[CREATE] `docs/security/evil-test-coverage-report.md`** — Gap analysis + attack surface map
**[CREATE] `backend/orchestrator/tests/test_evil_desktop.py`** — Desktop-specific evil tests (CSRF, session hijacking)

### Wave 6: Production Soak Plan + Execution

**[CREATE] `docs/operations/production-soak-plan.md`** — 24h soak test procedure, error rate targets, rollback triggers
**[CREATE] `backend/orchestrator/scripts/soak_test.py`** — 24h load test wrapper with monitoring hooks
**[CREATE] `docs/operations/soak-test-results.md`** — Live soak test results (appended post-execution)

### Wave 7: Carry-Forward Debt Closure

**[CREATE] `backend/orchestrator/services/presence_nonce_service.py`** — RED tier nonce binding (Phase 2.5 condition)
**[MODIFY] `backend/orchestrator/services/brain_layer.py`** — Add receipt generation for LLM calls
**[MODIFY] `Aspire-desktop/server/routes.ts`** — Bind actor_id in Desktop approval/deny receipts
**[MODIFY] `Aspire-desktop/server/admin.py`** — Add actor_id to admin ops receipts
**[CREATE] `backend/orchestrator/tests/test_presence_nonce.py`** — Nonce binding verification tests
**[CREATE] `backend/orchestrator/tests/test_brain_receipts.py`** — LLM call receipt coverage tests

### Wave 8: Incident Runbook Drill

**[CREATE] `docs/operations/incident-drill-p0-playbook.md`** — P0 scenario + response steps
**[CREATE] `docs/operations/incident-drill-p1-playbook.md`** — P1 scenario + response steps
**[CREATE] `docs/operations/incident-drill-results.md`** — Drill execution report (time-to-recovery, gaps found)

### Wave 9: Ship Verification

**[RUN] aspire-test-engineer** — Verify all new tests pass, coverage ≥80%
**[RUN] receipt-ledger-auditor** — Verify 100% receipt coverage for new code paths
**[RUN] security-reviewer** — Gate 5 checklist + evil test review
**[RUN] policy-gate-engineer** — Verify no shadow execution, fail-closed enforcement
**[RUN] output-critic** — Quality review + scope attribution check
**[RUN] release-sre** — Final PRR-Lite ship verdict (all 5 gates)

---

## 3) Contracts/Schemas Impacted

### Admin Portal Railway Config (NEW)

**Name:** `Aspire-admin-portal/railway.json`
**Current State:** Does not exist (admin portal not deployed)
**Proposed State:**
```json
{
  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "Aspire-desktop/Dockerfile"
  },
  "deploy": {
    "numReplicas": 1,
    "restartPolicyType": "ON_FAILURE",
    "healthcheckPath": "/api/admin/ops/health",
    "healthcheckTimeout": 30
  },
  "env": {
    "NODE_ENV": "production",
    "PORT": "3100",
    "SUPABASE_URL": "${{SUPABASE_URL}}",
    "SUPABASE_ANON_KEY": "${{SUPABASE_ANON_KEY}}",
    "ADMIN_JWT_SECRET": "${{ADMIN_JWT_SECRET}}",
    "ORCHESTRATOR_URL": "${{ORCHESTRATOR_URL}}"
  }
}
```
**Breaking Changes:** No (new service)

### Anam SDK Integration (MODIFY)

**Name:** `Aspire-desktop/lib/anam.ts`
**Current State:**
```typescript
export function getAnamPersonaId(): string {
  return process.env.EXPO_PUBLIC_ANAM_PERSONA_ID || 'a1em9A5J3l2W6tmBIFMmr';
}

export function getAnamFrameUrl(): string {
  return `${ANAM_BASE_URL}/${getAnamPersonaId()}`;
}
```

**Proposed State:**
```typescript
import { AnamClient } from '@anam-ai/js-sdk';  // NEW dependency

export function getAnamPersonaId(): string {
  return process.env.EXPO_PUBLIC_ANAM_PERSONA_ID || 'a1em9A5J3l2W6tmBIFMmr';
}

export function getAnamApiKey(): string {
  const key = process.env.EXPO_PUBLIC_ANAM_API_KEY;
  if (!key) throw new Error('EXPO_PUBLIC_ANAM_API_KEY not configured');
  return key;
}

export function createAnamClient(): AnamClient {
  return new AnamClient({
    apiKey: getAnamApiKey(),
    personaId: getAnamPersonaId(),
  });
}

// DEPRECATED: Use createAnamClient() instead
export function getAnamFrameUrl(): string {
  return `${ANAM_BASE_URL}/${getAnamPersonaId()}`;
}
```
**Breaking Changes:** No (getAnamFrameUrl kept for backward compatibility, deprecated)

### Load Test Real Services Request (NEW)

**Name:** Load test service integration payload
**Current State:** Load test uses mock/approval-only actions (no real provider calls)
**Proposed State:**
```typescript
{
  action: "twilio.sms.send",
  risk_tier: "yellow",
  provider: "twilio",
  sandbox_mode: true,
  payload: {
    to: "+15005550006",  // Twilio magic number (always succeeds)
    from: process.env.TWILIO_SANDBOX_NUMBER,
    body: "Load test message"
  },
  approval_binding: {
    approver_id: "load-test-runner",
    approved_at: Date.now(),
    payload_hash: "sha256(...)"
  }
}
```
**Breaking Changes:** No (additive to existing load test)

### Grafana Dashboard Schema (NEW)

**Name:** Dashboard JSON structure
**Current State:** No dashboards deployed
**Proposed State:** Standard Grafana dashboard JSON with:
- Prometheus data source (`http://prometheus:9090`)
- Panels: p50/p95/p99 latency (histogram queries)
- Panels: Error rate by risk tier (counter queries)
- Panels: Receipt throughput (gauge queries)
- Panels: RLS violation count (should be zero always)
- Templating: $suite_id selector for multi-tenancy

**Breaking Changes:** No (new dashboards)

### Presence Nonce Schema (MODIFY)

**Name:** `presence_tokens` table (migration 053)
**Current State:** Fields: token_id, suite_id, office_id, session_id, nonce, payload_hash, issued_at, expires_at, signature
**Proposed State:** Add `nonce_used_at` timestamp field for replay detection
**Migration:**
```sql
ALTER TABLE presence_tokens
ADD COLUMN nonce_used_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX idx_presence_nonce_replay
ON presence_tokens (nonce, nonce_used_at)
WHERE nonce_used_at IS NOT NULL;
```
**Breaking Changes:** No (additive, backward compatible)

### Brain Layer Receipt Schema (MODIFY)

**Name:** `receipts` table
**Current State:** receipt_type enum includes: 'tool_execution', 'approval', 'denial', 'policy_check', 'ops_*'
**Proposed State:** Add 'brain_layer_call' to receipt_type enum
**Migration:**
```sql
ALTER TYPE receipt_type ADD VALUE IF NOT EXISTS 'brain_layer_call';
```
**Breaking Changes:** No (enum extension, backward compatible)

### Desktop Approval Receipt Schema (MODIFY)

**Name:** Desktop approval receipt payload
**Current State:**
```json
{
  "receipt_id": "...",
  "correlation_id": "...",
  "suite_id": "...",
  "risk_tier": "yellow",
  "actor_type": "user",
  "title": "..."
}
```

**Proposed State:**
```json
{
  "receipt_id": "...",
  "correlation_id": "...",
  "suite_id": "...",
  "office_id": "...",
  "risk_tier": "yellow",
  "actor_type": "user",
  "actor_id": "user-uuid-from-jwt",  // NEW: binds approval to specific user
  "title": "...",
  "approved_at": "ISO8601",
  "approval_method": "desktop_ui"
}
```
**Breaking Changes:** No (additive fields)

---

## 4) Step-by-Step Build Plan

### Wave 1: Admin Portal Deploy (Railway + DNS)
**Parallel Group:** None (sequential dependency for Wave 2-8)
**Complexity:** M (120 min)

**Step 1.1:** Create Railway config — `Aspire-admin-portal/railway.json`
**Files:** `Aspire-admin-portal/railway.json` (NEW)
**Done Condition:** railway.json validates with Railway CLI

**Step 1.2:** Configure GoDaddy DNS CNAME — admin.aspireos.app → Railway app domain
**Files:** External (GoDaddy DNS panel)
**Done Condition:** `nslookup admin.aspireos.app` resolves to Railway IP

**Step 1.3:** Deploy admin portal to Railway
**Files:** Railway dashboard
**Done Condition:** `/api/admin/ops/health` returns 200 from admin.aspireos.app

**Step 1.4:** Create deployment checklist
**Files:** `docs/operations/admin-portal-deploy.md`
**Done Condition:** Checklist includes pre-deploy, deploy, verify, rollback steps

**Step 1.5:** Playwright smoke test for deployed admin portal
**Files:** `backend/tests/e2e/playwright/admin-portal-health.spec.ts`
**Done Condition:** Smoke test passes against admin.aspireos.app

### Wave 2: Anam Avatar Integration
**Parallel Group:** Can run parallel with Wave 3, 4
**Complexity:** M (90 min)

**Step 2.1:** Add Anam SDK dependency
**Files:** `Aspire-desktop/package.json`
**Done Condition:** `pnpm install` succeeds, @anam-ai/js-sdk in node_modules

**Step 2.2:** Create Anam client helpers
**Files:** `Aspire-desktop/lib/anam.ts`
**Done Condition:** `createAnamClient()` function exported, uses EXPO_PUBLIC_ANAM_API_KEY

**Step 2.3:** Create useAnamAvatar React hook
**Files:** `Aspire-desktop/hooks/useAnamAvatar.ts`
**Done Condition:** Hook manages session lifecycle (connect, disconnect, error handling)

**Step 2.4:** Replace iframe with Anam SDK in AvaDeskPanel
**Files:** `Aspire-desktop/components/desktop/AvaDeskPanel.tsx`
**Done Condition:** "Video with Ava" toggle renders Anam component (NOT iframe)

**Step 2.5:** Playwright test for Anam avatar rendering
**Files:** `backend/tests/e2e/playwright/ava-anam-avatar.spec.ts`
**Done Condition:** Test verifies Anam component visible when Video mode active

### Wave 3: Load Test Execution
**Parallel Group:** Can run parallel with Wave 2, 4
**Complexity:** L (150 min)

**Step 3.1:** Create real services load test harness
**Files:** `backend/orchestrator/scripts/load_test_real_services.py`
**Done Condition:** Script accepts --providers flag (twilio, stripe, elevenlabs, livekit)

**Step 3.2:** Add Twilio sandbox integration
**Files:** `backend/orchestrator/scripts/load_test_real_services.py`
**Done Condition:** Script sends SMS to Twilio magic number (+15005550006)

**Step 3.3:** Add Stripe test mode integration
**Files:** `backend/orchestrator/scripts/load_test_real_services.py`
**Done Condition:** Script creates Stripe test invoice

**Step 3.4:** Add ElevenLabs integration
**Files:** `backend/orchestrator/scripts/load_test_real_services.py`
**Done Condition:** Script synthesizes voice (Ava voice ID 56bWURjYFHyYyVf490Dp)

**Step 3.5:** Add LiveKit integration
**Files:** `backend/orchestrator/scripts/load_test_real_services.py`
**Done Condition:** Script creates LiveKit room

**Step 3.6:** Execute load test (10 min, 100 req/min)
**Files:** Command line execution
**Done Condition:** Load test completes, JSON report generated

**Step 3.7:** Document results
**Files:** `docs/operations/load-test-real-services-report.md`
**Done Condition:** Report includes p50/p95/p99, error rate, SLO verdict

### Wave 4: Grafana Dashboard Deployment
**Parallel Group:** Can run parallel with Wave 2, 3
**Complexity:** M (90 min)

**Step 4.1:** Create Desktop dashboard JSON
**Files:** `infrastructure/grafana/dashboards/aspire-desktop.json`
**Done Condition:** Dashboard includes latency, error rate, request count panels

**Step 4.2:** Create Orchestrator dashboard JSON
**Files:** `infrastructure/grafana/dashboards/aspire-orchestrator.json`
**Done Condition:** Dashboard includes p50/p95/p99, tool success rate panels

**Step 4.3:** Create Receipts dashboard JSON
**Files:** `infrastructure/grafana/dashboards/aspire-receipts.json`
**Done Condition:** Dashboard includes throughput, chain integrity, RLS violation panels

**Step 4.4:** Configure dashboard provisioning
**Files:** `infrastructure/grafana/provisioning/dashboards.yml`
**Done Condition:** Dashboards auto-import on Grafana startup

**Step 4.5:** Document Grafana setup
**Files:** `docs/operations/grafana-setup.md`
**Done Condition:** Doc includes dashboard import, Prometheus data source config

**Step 4.6:** Verify dashboards live
**Files:** Grafana UI (localhost:3000)
**Done Condition:** All 3 dashboards visible, showing live metrics

### Wave 5: Evil Test Gap Analysis + Execution
**Parallel Group:** None (depends on Wave 3 for load test evil scenarios)
**Complexity:** M (120 min)

**Step 5.1:** Gap analysis — identify missing attack vectors
**Files:** `docs/security/evil-test-coverage-report.md`
**Done Condition:** Report lists 10 attack categories, coverage % per category

**Step 5.2:** Add missing evil tests to backend
**Files:** `backend/orchestrator/tests/test_evil_security.py`
**Done Condition:** 5 new evil tests added (webhook replay, rate limiting bypass, IDOR)

**Step 5.3:** Create Desktop-specific evil tests
**Files:** `backend/orchestrator/tests/test_evil_desktop.py`
**Done Condition:** CSRF, session hijacking, XSS tests added

**Step 5.4:** Execute all evil tests
**Files:** pytest command line
**Done Condition:** All evil tests pass (zero regressions)

**Step 5.5:** Update coverage report
**Files:** `docs/security/evil-test-coverage-report.md`
**Done Condition:** Coverage report shows 100% for implemented attack vectors

### Wave 6: Production Soak Plan + Execution
**Parallel Group:** None (depends on Wave 3 load test completion)
**Complexity:** L (24h + 2h setup/analysis)

**Step 6.1:** Create soak plan document
**Files:** `docs/operations/production-soak-plan.md`
**Done Condition:** Plan specifies 24h duration, error rate < 1%, rollback triggers

**Step 6.2:** Create soak test wrapper script
**Files:** `backend/orchestrator/scripts/soak_test.py`
**Done Condition:** Script runs load_test.py for 24h, monitors error rate every 15 min

**Step 6.3:** Execute soak test (24h)
**Files:** Command line execution
**Done Condition:** Soak test completes 24h, final report generated

**Step 6.4:** Document results
**Files:** `docs/operations/soak-test-results.md`
**Done Condition:** Report includes error rate timeline, SLO compliance verdict

### Wave 7: Carry-Forward Debt Closure
**Parallel Group:** Can run parallel with Wave 5, 6
**Complexity:** L (180 min)

**Step 7.1:** Implement presence nonce service
**Files:** `backend/orchestrator/services/presence_nonce_service.py`
**Done Condition:** Nonce binding enforced, replay detection active

**Step 7.2:** Add Brain Layer receipt generation
**Files:** `backend/orchestrator/services/brain_layer.py`
**Done Condition:** Every LLM call emits receipt with prompt_hash, response_hash

**Step 7.3:** Bind actor_id in Desktop receipts
**Files:** `Aspire-desktop/server/routes.ts`
**Done Condition:** Approval/deny receipts include actor_id from JWT

**Step 7.4:** Bind actor_id in admin receipts
**Files:** `Aspire-desktop/server/admin.py`
**Done Condition:** All ops receipts include actor_id

**Step 7.5:** Create presence nonce tests
**Files:** `backend/orchestrator/tests/test_presence_nonce.py`
**Done Condition:** 10 tests pass (replay, expiry, cross-tenant)

**Step 7.6:** Create Brain receipts tests
**Files:** `backend/orchestrator/tests/test_brain_receipts.py`
**Done Condition:** 5 tests pass (LLM call coverage, redaction)

**Step 7.7:** Create migration for nonce_used_at
**Files:** `backend/supabase/migrations/053_presence_nonce_replay.sql`
**Done Condition:** Migration applies without errors

**Step 7.8:** Create migration for brain_layer_call receipt type
**Files:** `backend/supabase/migrations/054_brain_receipt_type.sql`
**Done Condition:** Migration applies without errors

### Wave 8: Incident Runbook Drill
**Parallel Group:** None (final verification before ship)
**Complexity:** M (120 min)

**Step 8.1:** Create P0 incident playbook
**Files:** `docs/operations/incident-drill-p0-playbook.md`
**Done Condition:** Playbook includes scenario, steps, time targets

**Step 8.2:** Create P1 incident playbook
**Files:** `docs/operations/incident-drill-p1-playbook.md`
**Done Condition:** Playbook includes scenario, steps, time targets

**Step 8.3:** Execute P0 drill (simulated payroll failure)
**Files:** Manual execution
**Done Condition:** Team executes playbook, records time-to-recovery

**Step 8.4:** Execute P1 drill (simulated provider outage)
**Files:** Manual execution
**Done Condition:** Team executes playbook, records time-to-recovery

**Step 8.5:** Document drill results
**Files:** `docs/operations/incident-drill-results.md`
**Done Condition:** Results include gaps found, action items with owners

### Wave 9: Ship Verification (All Agents)
**Parallel Group:** None (final gate)
**Complexity:** L (240 min — agent runtime)

**Step 9.1:** Run aspire-test-engineer agent
**Files:** Agent output → `docs/agents/test-engineer/phase3-groupc-gate1.md`
**Done Condition:** All tests pass, coverage ≥80%

**Step 9.2:** Run receipt-ledger-auditor agent
**Files:** Agent output → `docs/agents/receipt-auditor/phase3-groupc-receipt-audit.md`
**Done Condition:** 100% receipt coverage for new code paths

**Step 9.3:** Run security-reviewer agent
**Files:** Agent output → `docs/agents/security-reviewer/phase3-groupc-gate5.md`
**Done Condition:** Gate 5 checklist complete, evil tests reviewed

**Step 9.4:** Run policy-gate-engineer agent
**Files:** Agent output → `docs/agents/policy-gate/phase3-groupc-policy-review.md`
**Done Condition:** No shadow execution, fail-closed verified

**Step 9.5:** Run output-critic agent
**Files:** Agent output → `docs/agents/output-critic/phase3-groupc-review.md`
**Done Condition:** Quality review complete, scope attribution correct

**Step 9.6:** Run release-sre agent (final PRR-Lite)
**Files:** Agent output → `docs/agents/release-sre/phase3-groupc-ship-verdict.md`
**Done Condition:** Ship verdict = SHIP (all 5 gates PASS)

---

## 5) Test Plan

### Unit Tests

**UNIT** `test_anam_client_init` — Anam client initializes with valid API key
**UNIT** `test_anam_client_missing_key_fails` — Anam client throws error if EXPO_PUBLIC_ANAM_API_KEY missing
**UNIT** `test_presence_nonce_binding` — Nonce binds to payload_hash, replay rejected
**UNIT** `test_brain_layer_receipt_generation` — LLM call emits receipt with prompt_hash
**UNIT** `test_desktop_actor_id_binding` — Approval receipt includes actor_id from JWT
**UNIT** `test_load_test_real_twilio` — Load test Twilio helper sends SMS to magic number
**UNIT** `test_load_test_real_stripe` — Load test Stripe helper creates test invoice
**UNIT** `test_load_test_real_elevenlabs` — Load test ElevenLabs helper synthesizes voice
**UNIT** `test_load_test_real_livekit` — Load test LiveKit helper creates room

### Integration Tests

**INT** `test_admin_portal_health_endpoint` — /api/admin/ops/health returns 200 with status
**INT** `test_anam_avatar_render_in_desktop` — AvaDeskPanel renders Anam component when Video mode active
**INT** `test_grafana_dashboards_import` — Grafana auto-provisions 3 dashboards on startup
**INT** `test_load_test_orchestrator_integration` — Load test hits orchestrator /v1/intents, returns valid JSON
**INT** `test_presence_nonce_orchestrator_flow` — RED tier action enforces nonce binding end-to-end

### E2E Tests

**E2E** `test_admin_portal_smoke` — Navigate to admin.aspireos.app, verify health OK
**E2E** `test_ava_video_mode_anam_avatar` — Click "Video with Ava", verify Anam avatar renders
**E2E** `test_desktop_approval_with_actor_id` — Approve action, verify receipt includes actor_id
**E2E** `test_grafana_desktop_dashboard` — Open Grafana, verify Desktop dashboard shows live metrics
**E2E** `test_soak_test_24h` — Run soak test for 24h, verify error rate < 1%

### Negative Tests (MANDATORY)

**NEG** `test_anam_invalid_api_key_denied` — Anam client with invalid API key fails gracefully
**NEG** `test_load_test_orchestrator_unavailable` — Load test handles orchestrator 503 errors
**NEG** `test_presence_nonce_replay_denied` — Reusing nonce is rejected with reason_code
**NEG** `test_brain_layer_no_receipt_fails` — LLM call without receipt emission triggers alert
**NEG** `test_admin_portal_missing_jwt_denied` — Admin portal rejects requests without valid JWT
**NEG** `test_grafana_no_prometheus_fails` — Grafana dashboard shows "No data" if Prometheus down
**NEG** `test_desktop_approval_no_actor_id_fails` — Approval without actor_id binding is rejected

### Evil Tests (if security-critical)

**EVIL** `test_anam_session_hijacking_blocked` — Stolen Anam session token rejected (signature mismatch)
**EVIL** `test_load_test_rate_limit_bypass_blocked` — Load test cannot exceed orchestrator rate limits
**EVIL** `test_desktop_csrf_attack_blocked` — CSRF token validation prevents cross-origin approval
**EVIL** `test_grafana_unauthenticated_access_denied` — Grafana dashboard requires auth (no public access)
**EVIL** `test_presence_nonce_cross_tenant_blocked` — Nonce for Suite A rejected when used by Suite B

---

## 6) Risk Register

### R1: Anam SDK Integration Complexity
**Severity:** Medium
**Likelihood:** Medium
**Impact:** Anam SDK may have undocumented quirks, breaking Desktop Video mode
**Mitigation:**
- Use Anam official documentation + examples from Anam lab
- Implement error boundary around Anam component (graceful degradation to iframe)
- Add comprehensive logging for Anam session lifecycle events
- Fallback to iframe if Anam SDK initialization fails
**Residual Risk:** Low (iframe fallback preserves basic functionality)

### R2: Load Test Against Real Services May Hit Rate Limits
**Severity:** High
**Likelihood:** Medium
**Impact:** Twilio/Stripe/ElevenLabs rate limits cause load test false failures, blocking ship
**Mitigation:**
- Use Twilio sandbox magic numbers (unlimited sends)
- Use Stripe test mode (no rate limits)
- Throttle ElevenLabs calls to stay within free tier (10 req/min)
- LiveKit cloud has generous free tier (100 rooms/month)
- Run load test at reduced rate (100 req/min, NOT 1000 req/min)
- Add retry logic with exponential backoff for 429 errors
**Residual Risk:** Medium (rate limits may still trigger, requires manual verification)

### R3: Grafana Dashboard Deployment May Fail on Railway
**Severity:** Medium
**Likelihood:** Low
**Impact:** Dashboards don't auto-import, manual import required (delays Gate 2 compliance)
**Mitigation:**
- Use Grafana provisioning YAML (standard mechanism)
- Test provisioning locally before Railway deploy
- Document manual import procedure as fallback
- Verify dashboards in staging environment first
**Residual Risk:** Low (manual import is acceptable workaround)

### R4: 24h Soak Test May Reveal Unknown Stability Issues
**Severity:** Critical
**Likelihood:** Low
**Impact:** Soak test reveals memory leak, connection pool exhaustion, or database deadlock — blocks ship
**Mitigation:**
- Run shorter soak tests first (1h, 6h) to catch obvious issues
- Monitor memory usage, connection pool stats, DB query times
- Set error rate threshold (1%) — fail fast if exceeded
- Have rollback plan ready (revert to prior stable build)
- Run soak test against Railway staging (NOT production)
**Residual Risk:** Medium (unknown unknowns may emerge)

### R5: Carry-Forward Debt Closure May Introduce Regressions
**Severity:** High
**Likelihood:** Medium
**Impact:** Presence nonce or Brain Layer receipts break existing flows, causing test failures
**Mitigation:**
- Implement presence nonce as additive (opt-in for RED tier, not enforced globally)
- Brain Layer receipts use new receipt_type (no conflict with existing types)
- Run full test suite after each debt closure step
- Isolate changes to dedicated services (presence_nonce_service.py, brain_layer.py)
- Add feature flag for presence nonce (disable if issues found)
**Residual Risk:** Low (isolated changes, full test coverage)

---

## 7) Gate Checklist

### Gate 0 (Development Complete)

**Wave 1: Admin Portal Deploy**
- [ ] All files from Section 2 Wave 1 created/modified
- [ ] Railway config validates with Railway CLI
- [ ] GoDaddy DNS CNAME configured
- [ ] Admin portal deploys to Railway successfully
- [ ] /api/admin/ops/health returns 200 from admin.aspireos.app
- [ ] Deployment checklist documented
- [ ] Playwright smoke test passes

**Wave 2: Anam Avatar Integration**
- [ ] All files from Section 2 Wave 2 created/modified
- [ ] Anam SDK dependency installed
- [ ] Anam client helpers implemented
- [ ] useAnamAvatar hook implemented
- [ ] AvaDeskPanel uses Anam SDK (NOT iframe)
- [ ] Playwright test verifies Anam avatar renders

**Wave 3: Load Test Execution**
- [ ] All files from Section 2 Wave 3 created/modified
- [ ] Load test harness supports --providers flag
- [ ] Twilio sandbox integration working
- [ ] Stripe test mode integration working
- [ ] ElevenLabs integration working
- [ ] LiveKit integration working
- [ ] Load test executes successfully (10 min, 100 req/min)
- [ ] Results documented with p50/p95/p99, error rate, SLO verdict

**Wave 4: Grafana Dashboard Deployment**
- [ ] All files from Section 2 Wave 4 created/modified
- [ ] Desktop dashboard JSON created
- [ ] Orchestrator dashboard JSON created
- [ ] Receipts dashboard JSON created
- [ ] Dashboard provisioning configured
- [ ] Grafana setup documented
- [ ] All 3 dashboards visible in Grafana UI

**Wave 5: Evil Test Gap Analysis**
- [ ] All files from Section 2 Wave 5 created/modified
- [ ] Gap analysis report completed
- [ ] 5 new evil tests added to test_evil_security.py
- [ ] Desktop-specific evil tests added
- [ ] All evil tests pass (zero regressions)
- [ ] Coverage report updated

**Wave 6: Production Soak Plan**
- [ ] All files from Section 2 Wave 6 created/modified
- [ ] Soak plan document created
- [ ] Soak test wrapper script implemented
- [ ] 24h soak test executed
- [ ] Results documented with error rate timeline, SLO verdict

**Wave 7: Carry-Forward Debt Closure**
- [ ] All files from Section 2 Wave 7 created/modified
- [ ] Presence nonce service implemented
- [ ] Brain Layer receipts implemented
- [ ] Desktop actor_id binding implemented
- [ ] Admin actor_id binding implemented
- [ ] Presence nonce tests pass (10 tests)
- [ ] Brain receipts tests pass (5 tests)
- [ ] Migrations applied successfully

**Wave 8: Incident Runbook Drill**
- [ ] All files from Section 2 Wave 8 created/modified
- [ ] P0 playbook created
- [ ] P1 playbook created
- [ ] P0 drill executed
- [ ] P1 drill executed
- [ ] Drill results documented

**Wave 9: Ship Verification**
- [ ] aspire-test-engineer agent complete
- [ ] receipt-ledger-auditor agent complete
- [ ] security-reviewer agent complete
- [ ] policy-gate-engineer agent complete
- [ ] output-critic agent complete
- [ ] release-sre agent complete (final verdict)

**General (All Waves):**
- [ ] No shadow execution paths introduced
- [ ] Receipt coverage: 100% of state-changing operations
- [ ] All tests from Section 5 written and passing
- [ ] Code follows existing patterns (see MEMORY.md)

### Gate 1 (Integration Verified)

- [ ] RLS isolation tests passing (zero cross-tenant leakage)
- [ ] Evil tests passing (all attack vectors covered)
- [ ] Canonical execution path verified end-to-end
- [ ] Idempotency verified for all provider writes
- [ ] Fail-closed behavior verified for missing policy/tokens
- [ ] Correlation IDs flow through all new code paths
- [ ] Code coverage ≥ 80% for new code
- [ ] Admin portal smoke test passes against admin.aspireos.app
- [ ] Anam avatar renders in Desktop Video mode
- [ ] Load test against real services passes SLO targets
- [ ] Grafana dashboards show live metrics

### Gate 2 (Observability Complete)

- [ ] SLO definitions documented (Desktop latency, error rate)
- [ ] Grafana dashboards deployed (Desktop, Orchestrator, Receipts)
- [ ] Prometheus scraping all services (Desktop, Orchestrator, Receipts)
- [ ] Health checks present (liveness, readiness) on all services
- [ ] Correlation IDs flow Desktop → Orchestrator → Receipts
- [ ] Alert routing configured (who gets paged for failures)
- [ ] Log retention policy defined (30 days minimum)

### Gate 3 (Reliability Hardened)

- [ ] Circuit breaker implemented for Desktop → Orchestrator proxy
- [ ] Timeout enforcement (<5s Desktop, <30s Orchestrator)
- [ ] Exponential backoff with jitter for retries
- [ ] Idempotency keys used for all provider writes
- [ ] Load test passes with error rate < 1%
- [ ] Soak test (24h) passes with error rate < 1%

### Gate 4 (Operations Ready)

- [ ] Incident runbooks documented (P0, P1 playbooks)
- [ ] Runbook drills executed (P0, P1 scenarios)
- [ ] Postmortem template ready
- [ ] Rollback procedure documented
- [ ] Rollback procedure tested (simulated bad deploy)
- [ ] Deployment checklist documented (admin portal)
- [ ] Smoke test suite automated (Playwright)

### Gate 5 (Security Hardened)

- [ ] Security review checklist complete (5 pillars)
- [ ] Evil tests comprehensive (all attack vectors)
- [ ] Secrets management verified (no hardcoded keys)
- [ ] DLP/PII redaction active (Presidio in Ops Telemetry)
- [ ] Tenant isolation verified (RLS tests pass)
- [ ] Network boundary verified (no public MCP tools)
- [ ] Shadow execution prevention verified (canonical path enforced)

### Ship Readiness (All Gates Must Pass)

- [ ] Gate 1: PASS
- [ ] Gate 2: PASS
- [ ] Gate 3: PASS
- [ ] Gate 4: PASS
- [ ] Gate 5: PASS
- [ ] Release SRE verdict: SHIP (unconditional)
- [ ] All carry-forward debt from prior phases closed
- [ ] No P0/P1 bugs open
- [ ] Production deployment plan reviewed by team

---

## Notes for Implementation

### Parallelization Strategy

**Parallel Group A (Wave 2, 3, 4):** Can execute simultaneously — no dependencies
**Sequential (Wave 1 → Group A):** Admin portal deploy must complete before load test (need admin.aspireos.app live)
**Parallel Group B (Wave 5, 7):** Can execute simultaneously after Wave 3 (load test provides evil test scenarios)
**Sequential (Group A → Wave 6):** Soak test requires load test completion
**Sequential (All → Wave 8 → Wave 9):** Final verification gates

### Critical Path

1. **Wave 1** (Admin Portal) — BLOCKING for all downstream work
2. **Wave 3** (Load Test) — BLOCKING for Wave 6 (soak test)
3. **Wave 6** (Soak Test) — BLOCKING for ship (24h duration)
4. **Wave 9** (Ship Verification) — FINAL GATE

**Estimated Total Duration:** 5 days (Wave 1: 2h, Wave 2-4 parallel: 4h, Wave 5-7 parallel: 6h, Wave 6: 24h, Wave 8: 2h, Wave 9: 4h) + 1 day buffer = **6 days**

### Environment Variables Required (User Must Provide)

```bash
# Railway env vars for Desktop + Admin Portal
TWILIO_ACCOUNT_SID=<your_twilio_sid>
TWILIO_AUTH_TOKEN=<your_twilio_token>
TWILIO_SANDBOX_NUMBER=<your_sandbox_number>
ELEVENLABS_API_KEY=<your_elevenlabs_key>
ANAM_API_KEY=<your_anam_key>
EXPO_PUBLIC_ANAM_PERSONA_ID=<cara_avatar_persona_id>
LIVEKIT_API_KEY=<your_livekit_key>
LIVEKIT_API_SECRET=<your_livekit_secret>
```

### Verification Agent Order

1. **test-engineer** — Verify tests pass, coverage ≥80%
2. **receipt-ledger-auditor** — Verify 100% receipt coverage
3. **security-reviewer** — Gate 5 checklist
4. **policy-gate-engineer** — No shadow execution, fail-closed
5. **output-critic** — Quality review, scope attribution
6. **release-sre** — Final PRR-Lite ship verdict

### Success Criteria

**Ship Verdict = SHIP (unconditional)** requires:
- All 5 Production Gates PASS
- Zero P0/P1 bugs open
- All carry-forward debt closed
- Load test + soak test both PASS SLO targets
- All verification agents report PASS
- Incident drills completed successfully

---

**END OF PLAN**
