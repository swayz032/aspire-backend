# Security Reviewer Agent Memory

## Pattern: Two-Track Migration Problem
- `backend/supabase/migrations/` = what's applied to production via `supabase db push` (12 files as of 2026-03-23)
- `backend/infrastructure/supabase/migrations/` = local dev/planning copies (086+ migrations exist here only)
- Fixes in `infrastructure/` are NOT automatically in `supabase/migrations/` — always cross-check both trees

## Confirmed Architecture Patterns
- RLS uses `current_setting('app.current_suite_id', true)::uuid` as the tenant gate
- SECURITY DEFINER functions: ~40+ across trust-spine bundle — all have `SET search_path` guard
- Token revocation: in-memory set (_revoked_tokens) in token_service.py — does not survive process restart
- DLP: Presidio primary, regex fallback, fail-closed on YELLOW/RED
- Capability tokens: HMAC-SHA256, 45s default TTL, 6-check validation

## Recurring Vulnerability Patterns Found

### SEC-01 (HIGH) — admin_allowlist open SELECT
- File: `supabase/migrations/20260318000001_platform_admin_rls.sql:102`
- Policy `admin_allowlist_select_authenticated` grants SELECT to ALL authenticated users — exposes admin emails
- Fix migration 086 exists in `infrastructure/` but NOT in `supabase/migrations/` (not yet applied to prod)

### SEC-02 (HIGH) — finance_knowledge INSERT/UPDATE policies lack role restriction
- File: `supabase/migrations/20260228000001_finance_knowledge_base.sql:127,131,201,204`
- `FOR INSERT WITH CHECK (true)` and `FOR UPDATE USING (true)` — no role restriction on authenticated users
- Any authenticated user can insert/modify finance knowledge chunks regardless of tenant
- Should be `TO service_role` only

### SEC-03 (MEDIUM) — XSS via document.write + template literals
- File: `Aspire-desktop/app/finance-hub/payroll/index.tsx:1356`
- Employee data (emp.name, emp.role, emp.rate) interpolated directly into `w.document.write(...)` HTML
- If any data field contains `<script>` or event handlers, it executes in the popup window
- Mitigated somewhat by data coming from the app's own state, not raw user input

### SEC-04 (LOW) — Token revocation not persistent
- File: `backend/orchestrator/src/aspire_orchestrator/services/token_service.py:45`
- `_revoked_tokens: set[str]` is in-memory only — revocations lost on process restart
- 45s TTL provides partial mitigation but creates a window

### SEC-05 (LOW) — MFA TOTP secret in localStorage fallback
- File: `Aspire-desktop/lib/security/mfa.ts:5` + `lib/security/storage.ts:13`
- On web, MFA secret stored in localStorage (not SecureStore which only works on native)
- localStorage is accessible to any JS on the same origin (XSS risk)

### SEC-06 (MEDIUM) — allow_internal_routing gated only by header presence, not validated JWT
- File: `backend/orchestrator/src/aspire_orchestrator/routes/intents.py:344`
- `allow_internal_routing = bool(request.headers.get("x-admin-token"))` — presence of header, not validity
- JWT validation happens in _require_admin() (called separately) but NOT at this point
- An attacker sending any x-admin-token value gets internal routing enabled

## Tables with Broad INSERT/UPDATE policies (cross-tenant writable)
- `finance_knowledge_chunks` — any authenticated user can INSERT/UPDATE
- `finance_knowledge_sources` — same issue
- Note: Partially mitigated by SECURITY DEFINER search functions enforcing suite_id at read time

## Security-Critical Hotspots
- `backend/orchestrator/src/aspire_orchestrator/services/token_service.py` — token mint/validate
- `backend/orchestrator/src/aspire_orchestrator/routes/admin.py` — 48K file, admin auth
- `backend/orchestrator/src/aspire_orchestrator/routes/intents.py` — allow_internal_routing logic
- `backend/supabase/migrations/20260318000001_platform_admin_rls.sql` — platform admin RLS
- `Aspire-desktop/app/finance-hub/payroll/index.tsx` — XSS via document.write
