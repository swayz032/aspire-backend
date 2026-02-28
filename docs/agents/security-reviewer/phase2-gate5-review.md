# 🔒 ASPIRE SECURITY REVIEW

**Scope:** Phase 2 Orchestrator - Brain Layer, Admin API, 11 Skill Packs, 5 State Machines, Infrastructure Services
**Date:** 2026-02-14
**Reviewer:** Security Reviewer Agent
**Review Type:** Gate 5 Security (Production Readiness)

---

## 1. THREATS FOUND (Ranked by Severity)

### CRITICAL
*None found.*

### HIGH

- **[THREAT-001]** Admin API JWT Secret Fail-Open in Development — `routes/admin.py:144-146` — In development mode, ANY non-empty token is accepted as valid admin credential without validation. This allows any attacker who can reach the endpoint to gain admin access.

- **[THREAT-002]** Cross-Suite Receipt Access in Admin API — `routes/admin.py:479-483` — Admin endpoint `/admin/ops/receipts` can query receipts across ALL suites when `suite_id` parameter is omitted, reading directly from in-memory store `_receipts` without enforcing suite_id filter. This violates Law #6 (Tenant Isolation) for admin operations.

- **[THREAT-003]** Metrics Endpoint CORS Bypass — `server.py:153-163` — `/metrics` endpoint relies on client IP address check for access control, but does not validate against CORS-allowed origins. An attacker from an allowed CORS origin (Gateway) could proxy requests to `/metrics` and bypass the IP restriction.

### MEDIUM

- **[THREAT-004]** Admin Token Not Logged in Receipts — `routes/admin.py:215-246` — Admin access receipts store `actor_id` but do not log the token used (even truncated hash). This makes it impossible to audit which specific admin credential was used for an operation, reducing forensic capability.

- **[THREAT-005]** DLP Fail-Open for GREEN Tier — `services/dlp.py:141-143` — If Presidio DLP initialization fails, `redact_text()` returns unredacted text with only a warning. GREEN tier operations continue with PII exposure. While acceptable per policy for GREEN, no escalation path exists if DLP becomes unavailable.

- **[THREAT-006]** Presence Token Logging Truncation — `services/presence_service.py:234-292` — Presence token validation failures log `token_id[:8]` (truncated). While safe, the truncation is inconsistent with capability token logging pattern and could hinder correlation during incident investigation.

### LOW

- **[THREAT-007]** In-Memory Admin Stores Not Tenant-Isolated — `routes/admin.py:48-52` — In-memory stores `_incidents`, `_provider_calls`, `_proposals` do not enforce suite_id scoping during write operations (`register_incident`, etc.). Any component can write cross-tenant data. Mitigated by Phase 2 scope (no production writes to these stores yet).

- **[THREAT-008]** Correlation ID Generation Uses uuid4 — Multiple files — Correlation IDs use `uuid.uuid4()` which is cryptographically random but traceable across systems if logged externally. Consider `uuid.uuid7()` (timestamp-based) for better time-ordering in logs.

---

## 2. EXPLOIT PATHS ATTEMPTED (≥ 5)

### Exploit #1: Admin API Dev Token Bypass
- **Attack Vector:** Attacker discovers orchestrator is running in `ASPIRE_ENV=development` mode (default), sends request to `/admin/ops/receipts` with `X-Admin-Token: attacker-controlled-string`
- **Preconditions:**
  - Orchestrator deployed with `ASPIRE_ENV=development` (default if not set)
  - Attacker can reach orchestrator directly (bypassing Gateway)
- **Steps:**
  1. Send `GET /admin/ops/receipts` with `X-Admin-Token: any-string`
  2. `_require_admin()` at line 144 checks `env == "development"` → returns `"admin-dev"`
  3. Endpoint returns ALL receipts across all tenants (line 479-483 cross-suite query)
  4. Attacker extracts PII, correlation IDs, suite IDs from receipts
- **Expected Outcome:** Attacker gains read access to all tenant receipts + admin operations
- **Actual Outcome:** **EXPLOITABLE** (HIGH severity)
- **Evidence:** `routes/admin.py:144-146` (dev mode bypass), `routes/admin.py:479-483` (cross-suite read)
- **Severity:** HIGH

### Exploit #2: Metrics Endpoint Scraping via Gateway Proxy
- **Attack Vector:** Attacker compromises a service behind the Gateway (localhost:3100) or exploits SSRF in Gateway, uses it to proxy requests to `/metrics`
- **Preconditions:**
  - Attacker controls a process/service that can make HTTP requests from `localhost` or CORS-allowed origin
  - `ASPIRE_METRICS_ALLOW_EXTERNAL` not set (default)
- **Steps:**
  1. From compromised Gateway process (localhost:3100), send `GET http://localhost:8000/metrics`
  2. Request appears to come from `127.0.0.1` → passes IP check (line 155)
  3. Prometheus metrics exposed containing suite counts, error rates, token mint counts
- **Expected Outcome:** Attacker extracts operational metrics (tenant activity, error patterns)
- **Actual Outcome:** **PARTIALLY MITIGATED** — Requires Gateway compromise, but then metrics are exposed
- **Evidence:** `server.py:143-167` (IP-based restriction only)
- **Severity:** MEDIUM

### Exploit #3: PII Exfiltration via Unredacted GREEN Tier Logs
- **Attack Vector:** Attacker triggers GREEN tier action (e.g., `domain.search`) with PII in parameters, then DLP service is made unavailable (DoS on Presidio)
- **Preconditions:**
  - DLP service initialization fails or becomes unavailable
  - GREEN tier action contains PII (email, phone, SSN) in search query
- **Steps:**
  1. Cause DLP init failure (e.g., corrupt Presidio models, resource exhaustion)
  2. Send `POST /v1/intents` with `task_type: domain.search`, `payload.query: "john.doe@example.com SSN 123-45-6789"`
  3. DLP redaction fails → `redact_text()` returns original text (line 141-143)
  4. Receipt stored with unredacted PII in `redacted_inputs` field
  5. Admin queries receipts → PII exposed
- **Expected Outcome:** PII leakage in receipts/logs
- **Actual Outcome:** **EXPLOITABLE** for GREEN tier (MEDIUM severity, by design for GREEN but no escalation)
- **Evidence:** `services/dlp.py:141-143` (fail-open warning only)
- **Severity:** MEDIUM

### Exploit #4: Cross-Tenant Incident Injection
- **Attack Vector:** Malicious component calls `register_incident()` with fake suite_id to pollute another tenant's incident log
- **Preconditions:**
  - Component has access to admin routes module (internal service)
  - Phase 2 in-memory incident store active
- **Steps:**
  1. From internal service, call `register_incident({"suite_id": "victim-suite-id", "severity": "sev1", ...})`
  2. Incident stored in `_incidents` dict (line 66-67)
  3. Victim tenant admin queries `/admin/ops/incidents?suite_id=victim-suite-id`
  4. Fake incident appears in victim's incident list
- **Expected Outcome:** Cross-tenant incident pollution (false positives in monitoring)
- **Actual Outcome:** **MITIGATED** — Phase 2 scope: no production writes to in-memory stores yet. All incidents come from internal circuit breakers (trusted).
- **Evidence:** `routes/admin.py:55-68` (no suite_id validation on write)
- **Severity:** LOW (deferred to Phase 3 DB implementation)

### Exploit #5: Token Mint Shadow Execution
- **Attack Vector:** Skill pack or tool executor attempts to mint its own capability token without orchestrator approval
- **Preconditions:**
  - Skill pack has access to `token_service.mint_token()` function
  - Orchestrator graph bypassed
- **Steps:**
  1. Malicious skill pack imports `from aspire_orchestrator.services.token_service import mint_token`
  2. Calls `mint_token(suite_id=..., tool=..., scopes=[...])` directly
  3. Token minted with valid signature
  4. Skill pack uses token to call tool executor
- **Expected Outcome:** Autonomous tool execution without orchestrator approval
- **Actual Outcome:** **MITIGATED** — All skill packs are internal code (not external plugins). Code review shows no direct `mint_token()` calls outside `token_mint_node.py`. Architectural enforcement: graph is the only entry point.
- **Evidence:** `services/token_service.py` (function is module-level, not access-controlled), `skillpacks/*.py` (no imports of token_service found)
- **Severity:** LOW (architectural mitigation sufficient for Phase 2)

### Exploit #6: Suite ID Injection via Intent Classification Headers
- **Attack Vector:** Attacker sends malicious `X-Suite-Id` header to `/v1/intents/classify` to bypass tenant isolation
- **Preconditions:**
  - Gateway JWT validation compromised or bypassed
  - Attacker can craft arbitrary HTTP headers
- **Steps:**
  1. Send `POST /v1/intents/classify` with `X-Suite-Id: victim-suite-id`, `X-Office-Id: attacker-office-id`
  2. Route handler extracts `suite_id` from headers (line 152)
  3. Intent classification succeeds for victim's suite
  4. Attacker receives skill pack routing plan for victim's configured actions
- **Expected Outcome:** Cross-tenant action discovery (which skill packs are enabled)
- **Actual Outcome:** **MITIGATED** — Gateway is trusted boundary. Headers are auth-derived (JWT claims). Attack requires Gateway compromise. Defense-in-depth: orchestrator does not execute without valid approval+token binding to suite.
- **Evidence:** `routes/intents.py:152-154` (trusts Gateway headers)
- **Severity:** LOW (requires Gateway compromise)

### Exploit #7: Receipt Hash Chain Manipulation
- **Attack Vector:** Attacker modifies in-memory receipt, then triggers chain verification to detect tampering
- **Preconditions:**
  - Attacker has write access to in-memory `_receipts` list (requires code execution inside orchestrator process)
- **Steps:**
  1. Inject code to modify `_receipts[N]["outcome"] = "success"` (was "denied")
  2. Trigger `POST /v1/receipts/verify-run` with `suite_id`
  3. Chain verifier recomputes hash, detects mismatch
  4. But modified receipt persists in memory until restart
- **Expected Outcome:** Receipt tampering undetected until verification triggered
- **Actual Outcome:** **MITIGATED** — Requires process-level code execution (game over). Hash chain verification works as designed (detects tampering). Dual-write to Supabase (Wave 9) provides immutable external copy.
- **Evidence:** `services/receipt_chain.py:verify_chain()`, `services/receipt_store.py:172-191` (Supabase dual-write)
- **Severity:** LOW (requires RCE)

---

## 3. REQUIRED REMEDIATIONS

| ID | Threat | Remediation (Minimal) | Defense-in-Depth | Priority |
|----|--------|----------------------|------------------|----------|
| R-001 | THREAT-001 | Remove dev mode admin bypass. Require valid JWT in all environments. Fail-closed if `ASPIRE_ADMIN_JWT_SECRET` not set. | Add admin token rotation mechanism. Log all admin auth attempts (success/failure) to separate audit log. | IMMEDIATE |
| R-002 | THREAT-002 | Enforce suite_id filter in cross-suite admin query path. If admin cross-suite view is intentional, add explicit `allow_cross_suite=True` flag + audit log. | Implement admin scope model: global admin vs. suite-scoped admin. Require elevated approval for cross-suite reads. | HIGH |
| R-003 | THREAT-003 | Add `/metrics` to excluded paths in CORS middleware OR require separate auth header for metrics endpoint. | Use mTLS for Prometheus scraping. Bind metrics endpoint to separate internal-only port (e.g., :9090). | HIGH |
| R-004 | THREAT-004 | Add `token_hash: sha256(token)[:16]` to admin access receipts for audit correlation. | Store admin token metadata (issuer, expiry) in receipts. Implement admin token revocation list. | MEDIUM |
| R-005 | THREAT-005 | For YELLOW/RED tier, fail-closed if DLP unavailable (already in policy_engine design). Add health check alert if DLP init fails. | Implement DLP redundancy (fallback to regex-based redaction). Add DLP availability metric to `/readyz`. | MEDIUM |
| R-006 | THREAT-006 | Standardize token logging: always truncate to 8 chars for correlation. Document truncation pattern in code comments. | Use consistent `token_id[:8]` pattern across all token validation logs. | LOW |
| R-007 | THREAT-007 | Add suite_id validation to `register_incident()`, `register_provider_call()`, `register_proposal()`. Reject if caller suite_id != incident suite_id (except for system actor). | Move admin stores to Supabase with RLS policies in Phase 3. | LOW |
| R-008 | THREAT-008 | No action required (informational). Consider uuid7 in Phase 3 for time-ordered correlation IDs. | Document correlation ID format in API spec. | LOW |

---

## 4. ASPIRE LAW COMPLIANCE

| Law | Status | Notes |
|-----|--------|-------|
| #1 Single Brain | ✅ | LangGraph graph is sole decision authority. All skill packs checked: no autonomous decisions. Tool executor registry enforces "tools are hands." |
| #2 Receipts | ✅ | 100% receipt coverage verified. All nodes emit receipts. Admin API emits access receipts. Wave 8B filled gaps (GREEN auto-approve, token mint failure, A2A errors). |
| #3 Fail Closed | ⚠️ | **VIOLATION in admin API dev mode** (THREAT-001). All other paths fail-closed: missing signing key → deny, missing DLP (YELLOW/RED) → deny, missing auth → 401. |
| #4 Risk Tiers | ✅ | Policy matrix enforces GREEN/YELLOW/RED tiers. RED tier actions (finn_money_desk, milo_payroll, clara_legal) require dual approval + presence. Binding fields enforced. |
| #5 Capability Tokens | ✅ | Token mint restricted to orchestrator. Execute node performs full 6-check validation (signature, expiry, revocation, scope, suite, office) before execution. Tokens <60s TTL. |
| #6 Tenant Isolation | ⚠️ | **VIOLATION in admin cross-suite query** (THREAT-002). All other paths enforce suite_id scoping: `query_receipts()` filters by suite_id, `get_chain_receipts()` enforces suite_id, intake node derives suite_id from auth context (not client). |
| #7 Tools Are Hands | ✅ | Tool executor registry verified. No skill pack calls providers directly. All provider calls go through `execute_tool()`. Domain Rail client uses S2S HMAC auth. No retry logic in tools (orchestrator decides). |

---

## 5. PRODUCTION GATE 5 (Security) VERDICT

**Overall: CONDITIONAL ⚠️**

### Passing Criteria

- [x] Network boundary enforcement verified — CORS restricted to Gateway origins, S2S HMAC for Domain Rail
- [ ] **BLOCKING:** Credentials management verified — Admin dev mode bypass allows unauthorized access (THREAT-001)
- [x] Shadow execution prevention verified — No autonomous decisions in skill packs, graph is sole authority
- [ ] **BLOCKING:** Tenant isolation verified — Admin API can query cross-tenant receipts (THREAT-002)
- [x] Safe logging verified — PII redacted via DLP (Presidio), token logs truncated to 8 chars, no secrets in logs

### Blocking Issues

1. **THREAT-001 (HIGH):** Admin API dev mode bypass — ANY token accepted in development mode. **BLOCKS PRODUCTION.**
   **Fix:** Remove lines 144-146 in `routes/admin.py`. Always require JWT validation. Fail-closed if secret not configured.

2. **THREAT-002 (HIGH):** Admin cross-suite receipt query — Lines 479-483 in `routes/admin.py` read from `_receipts` without suite_id filter. **BLOCKS PRODUCTION.**
   **Fix:** Enforce suite_id filter even for admin. If cross-suite view is intentional, add explicit `allow_cross_suite=True` flag with separate approval + audit logging.

3. **THREAT-003 (MEDIUM promoted to BLOCKING):** Metrics endpoint CORS bypass — IP-based restriction insufficient. **BLOCKS PRODUCTION.**
   **Fix:** Add metrics endpoint to CORS exclusions OR bind to separate internal-only port (e.g., `:9090` for Prometheus).

### Advisory Issues

- **THREAT-004 (MEDIUM):** Admin token not logged in receipts — Reduces forensic capability. Add token hash to receipts.
- **THREAT-005 (MEDIUM):** DLP fail-open for GREEN tier — Add health check alert if DLP init fails.
- **THREAT-006 (LOW):** Presence token logging truncation — Standardize to 8-char pattern.
- **THREAT-007 (LOW):** In-memory admin stores not tenant-isolated — Defer to Phase 3 DB migration.
- **THREAT-008 (LOW):** Correlation ID uses uuid4 — Informational, consider uuid7 in Phase 3.

---

## 6. COMPLIANCE SUMMARY

**Strengths:**
- Excellent Law #1 enforcement (Single Brain): No shadow execution paths found across 11 skill packs
- Law #2 (Receipt Coverage): 100% verified, including error paths (Wave 8B hardening)
- Law #5 (Capability Tokens): Full 6-check validation in execute node (lines 113-143)
- Law #7 (Tools Are Hands): Tool executor registry properly enforces bounded execution
- Credential management: All API keys from environment (settings.py), no hardcoded secrets
- DLP integration: Presidio redaction active for PII patterns (SSN, CC, email, phone, address)

**Gaps:**
- Law #3 violation in admin API (dev mode bypass)
- Law #6 violation in admin cross-suite query
- Metrics endpoint access control relies on IP only (insufficient)

---

## 7. ADDITIONAL OBSERVATIONS

### Positive Findings

1. **Token Logging Safety:** All token logs truncate to 8 chars (`token_id[:8]`) — no full token exposure found
2. **Suite ID Derivation:** Intake node correctly derives `suite_id` from auth context, NOT client payload (lines 130-144)
3. **RED Tier Governance:** Finn Money Desk and Milo Payroll enforce dual approval, presence tokens, and binding fields
4. **Receipt Hash Chain:** Verification logic intact, Supabase dual-write provides immutable external copy
5. **Provider Client Safety:** All 16 provider clients fetch credentials from settings, fail-closed if missing
6. **No Autonomous Retries:** Skill packs do not retry failures — orchestrator decides (Law #7 compliant)

### Security Debt (Track for Phase 3)

1. In-memory stores (`_incidents`, `_provider_calls`, `_proposals`) will be replaced by Supabase with RLS
2. Admin token rotation mechanism not yet implemented (manual rotation only)
3. DLP redundancy (fallback regex-based redaction) not implemented
4. Metrics endpoint should be on separate port for production (Prometheus best practice)
5. Admin scope model (global vs. suite-scoped admin) deferred to Phase 3

---

## 8. RECOMMENDED IMMEDIATE ACTIONS (Before Production Deploy)

1. **Fix THREAT-001:** Remove admin dev mode bypass (routes/admin.py:144-146)
2. **Fix THREAT-002:** Enforce suite_id filter in admin cross-suite query (routes/admin.py:479-483)
3. **Fix THREAT-003:** Exclude /metrics from CORS OR bind to internal-only port
4. **Add health check:** `/readyz` should verify DLP availability (already implemented at line 127)
5. **Document admin intent:** If cross-suite admin access is intentional, document the security model and add audit logging

---

## 9. EVIDENCE REFERENCES

**Files Reviewed (64 total):**
- Core: `server.py`, `graph.py`, `state.py`
- Nodes (8): `intake.py`, `safety_gate.py`, `policy_eval.py`, `approval_check.py`, `token_mint.py`, `execute.py`, `receipt_write.py`, `respond.py`
- Services (22): `token_service.py`, `receipt_chain.py`, `dlp.py`, `receipt_store.py`, `a2a_service.py`, `approval_service.py`, `presence_service.py`, `policy_engine.py`, `intent_classifier.py`, `skill_router.py`, `tool_executor.py`, and 11 others
- Routes (2): `intents.py`, `admin.py`
- Skillpacks (11): `adam_research.py`, `nora_conference.py`, `tec_documents.py`, `sarah_front_desk.py`, `eli_inbox.py`, `quinn_invoicing.py`, `mail_ops_desk.py`, `teressa_books.py`, `finn_money_desk.py`, `milo_payroll.py`, `clara_legal.py`
- Providers (16): `stripe_client.py`, `brave_client.py`, `tavily_client.py`, `livekit_client.py`, `twilio_client.py`, `pandadoc_client.py`, `deepgram_client.py`, `elevenlabs_client.py`, `s3_client.py`, `polaris_email_client.py`, `quickbooks_client.py`, `moov_client.py`, `plaid_client.py`, `gusto_client.py`, and 2 others
- Config: `settings.py`, `policy_matrix.yaml`, `skill_pack_manifests.yaml`, `money_rules.yaml`

**Grep Patterns Used:**
- Hardcoded credentials: `(password|secret|api_key|token|credential)\s*=\s*["'][^"']+["']` — 0 findings (SAFE)
- PII patterns: `\b(ssn|social_security|credit_card|email|phone|address)\b` — All in policy redaction config (SAFE)
- Autonomous execution: `(decide|autonomous|retry|self\.execute|auto_approve)` — Only comments (SAFE)
- RLS bypass: `bypassPermissions|bypass_rls|SECURITY\s+DEFINER` — 0 findings (SAFE)
- Token logging: `logger\.(info|debug|warning|error).*token` — All truncated to 8 chars (SAFE)

---

**Security Reviewer Agent Memory Updated:**
- Admin API dev mode bypass pattern (THREAT-001)
- Cross-suite query pattern in admin facades (THREAT-002)
- Metrics endpoint IP-based access control pattern (THREAT-003)
- DLP fail-open behavior for GREEN tier (acceptable by design)
- Token logging truncation standard: 8 chars for correlation
