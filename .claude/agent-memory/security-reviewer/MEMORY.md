# Security Reviewer Memory — Aspire Platform

## Memory Index

- [patterns.md](./patterns.md) — Recurring vulnerability patterns, hotspot files, RLS gaps
- [findings_log.md](./findings_log.md) — Historical findings per cycle with status tracking

## Key Security Facts (Load Always)

### Architecture
- Backend: Python/LangGraph FastAPI orchestrator at `backend/orchestrator/src/aspire_orchestrator/`
- Desktop server: TypeScript/Express at `Aspire-desktop/server/`
- Admin portal: `import-my-portal-main/` — **NOT PRESENT ON DISK as of Cycle 5** (review gap)

### Critical Hotspot Files
- `Aspire-desktop/server/financeTokenStore.ts` — `getConnection()` queries `finance_connections` by `id` alone (no suite_id filter) — TENANT ISOLATION GAP
- `Aspire-desktop/server/routes.ts` — 6000+ lines, all major API routes; many booking/service routes pass `req.params.userId` directly to storage without cross-tenant check
- `Aspire-desktop/server/index.ts` — Auth middleware, DEV_BYPASS_AUTH guard (3-condition), TENANT_ISOLATION_VIOLATION detection
- `backend/orchestrator/.../services/dlp.py` — Presidio DLP with regex fallback; fail-closed on YELLOW/RED
- `backend/orchestrator/.../nodes/policy_eval.py` — 9-step policy engine, correct fail-closed pattern

### Known Tracked Threats (do not re-report as NEW)
- THREAT-001: x-suite-id spoofing (S2S HMAC needed)
- THREAT-002: allow_internal_routing param bypass — FIXED Cycle 3+4
- THREAT-003: Full token in checkpoint state — 45s TTL mitigates
- THREAT-004: Rate limit key spoofing via x-suite-id
- THREAT-005: Client-supplied task_type bypasses LLM classification
- THREAT-006: Session-level set_config bleeds across pooled connections
- THREAT-007: Desktop startup allows missing secrets

### RLS Patterns
- DB-layer: `set_config('app.current_suite_id', suiteId, false)` via `applyTenantContext()` in `tenantContext.ts`
- Migrations use `current_setting('app.current_suite_id')` — but `temporal_task_tokens` (mig 088) uses `app.suite_id` (INCONSISTENT key name)
- `finance_connections` and `finance_tokens` tables: no RLS enforcement in application-layer queries (tokenStore bypasses via service role / direct Drizzle)
- SECURITY DEFINER functions: 70+ instances across migrations — all appear to have `SET search_path = public` or equivalent guards

### PII Logging
- `routes.ts:545` — `logger.info('Beta signup: user created', { userId, email: email.trim() })` — email logged in plaintext (LAW #9 VIOLATION)
- Logger itself has no PII-stripping layer — caller responsibility only

### DEV_BYPASS_AUTH
- Triple-guard: `DEV_BYPASS_AUTH=true` AND `!SUPABASE_URL` AND `NODE_ENV !== 'production'`
- All 3 must be true — strong protection against accidental production bypass
- But env-var-only: no code-level assertion that it can never reach Railway

### Pass 18 — Ingestion + Telephony Routes (Pass 13-17)
- `/v1/ingest/document` and `/v1/ingest/aspire-calendar` LACK auth dependency in server.py (comment claims it exists — it does not). Both are publicly writable. CRITICAL.
- `routes/sarah.py:215` — `called_number` string interpolated into PostgREST filter without E.164 validation (HIGH injection surface post-HMAC). Signature IS first check — but no format guard after parsing.
- `services/twilio_provisioning.py:release_number()` — queries by `id` only, no scope binding. Tenant can release another tenant's number (HIGH).
- DLP not invoked on any of the 13 ingestion adapter paths — inbound SMS body, transcripts, contracts go into memory_objects without PII scan.
- All 4 new tables (migration 102): FORCE ROW LEVEL SECURITY confirmed, policy uses `request.jwt.claim.tenant_id`. Migration 103 adds constraints only (no new tables).
- Capability tokens: server-minted only (Express proxy in routes.ts:7938 uses TOKEN_SIGNING_SECRET). Frontend never holds signing key. TTL=45s. 6-check validation confirmed.

### Anam Session Store
- In-memory Map, 30-minute TTL, 5-minute cleanup interval
- Session key = Anam session token (opaque string from Anam API)
- `/api/ava/chat-stream` is a PUBLIC PATH (no JWT) — relies entirely on session store lookup
- If session_id can be predicted/guessed, attacker could impersonate another user's suite context

### Wave 5 Trust Hub Status-Callback Dispatch — Reviewed 2026-05-03
- BLOCKING: unknown Twilio status values (not `twilio-approved`, not `twilio-rejected`) sent with valid HMAC map `is_approved=False` and advance state to `profile_rejected`/`failed` — state corruption. Fix: explicit allowlist check before dispatch.
- `_STATE_RANK` missing `kyb_disputed` — returns -1, bypasses idempotency guard for profiles in that state.
- `failure_reason` (FailureReason from form) NOT forwarded as `twilio_rejection_reason` to `cut_trust_receipt` — audit trail gap for rejection events.
- `uvicorn proxy_headers=True` without `forwarded_allow_ips` allows X-Forwarded-Host spoofing affecting HMAC URL verification.
- `_TWILIO_SID_RE` recompiled on every request (hot path inefficiency, no security impact).
- Test coverage gaps: no test for unknown status (e.g., `twilio-pending`), no test for profile in `kyb_disputed` state, no test that `twilio_rejection_reason` is passed through. See findings_log.md Wave 5 section.

### Wave 3 Trust Hub KYB (routes/trust_hub.py) — Reviewed 2026-05-03
- PII guardrail `_assert_no_pii` in trust_receipts.py is CORRECT and fail-closed. Blocks email, phone_e164, first_name, last_name, dob, ssn_last4, ein, address_street, raw_business_name.
- `address_state` and `address_zip` are NOT in _FORBIDDEN_PII_KEYS — intentional (state/zip are not PII). `address_street` IS blocked.
- Vault names use `{tenant_id}:ein` / `{tenant_id}:rep_{idx}_dob` / `{tenant_id}:rep_{idx}_ssn_last4` — correct namespace per W1 R-004.
- CRITICAL GAP: `validate_token` does NOT verify `tenant_id` claim. Checks suite_id + office_id only (6 checks). A token minted for suite_id=X under tenant_id=A can be replayed by tenant_id=B if they share suite_id (design gap — same scope used in `_resolve_scope` but token doesn't carry tenant claim).
- MEDIUM: `resource_sid` from Twilio callback form is interpolated directly into PostgREST filter `f"{sid_column}=eq.{resource_sid}"` without UUID/SID format validation after HMAC pass. Injection surface post-HMAC.
- MEDIUM: `trust_profile_id` from DB (str(profile["id"])) is interpolated into PostgREST update filter without UUID re-validation. Low risk since source is DB, but not explicit.
- MEDIUM: Race condition on dispute_count: read-then-increment not atomic. Concurrent calls could see same count and both succeed.
- MEDIUM: `address_street` stored plaintext in `tenant_trust_profiles` DB column. Not in vault. Per CLAUDE.md Law #9, addresses are PII. By design (needed for Twilio submission) but worth flagging.
- LOW: `dob` regex `^\d{4}-\d{2}-\d{2}$` does not parse as real date — allows "9999-99-99". No semantic date validation.
- LOW: `_vault_delete_secret` is best-effort (logs warning, does NOT raise on failure). Crash after delete but before re-encrypt leaves column pointing at deleted vault entry — Law #3 concern.
- LOW: `legal_business_name` has no Unicode normalization/control-char filtering (min_length=2, max_length=120 only).
- Token wildcard scope `trust_hub.*` accepted by validate_token — overly broad if minted. Check that mint path never issues wildcard scopes.
- Status endpoint is correctly Green (no cap token) per plan — intentional design, not oversight.
- HMAC validation on status-callback is FIRST check before any DB operation — correct order.
- 409 race on duplicate KYB submit: pre-check then insert — TOCTOU window exists but handled by DB unique constraint → clean 409.
