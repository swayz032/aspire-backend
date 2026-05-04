---
name: Wave 3 Trust Hub Route Patterns
description: FastAPI route patterns, test isolation fixes, and Law compliance patterns for KYB intake (trust_hub.py)
type: project
---

# Wave 3 Trust Hub Route Patterns

## File locations
- Route: `backend/orchestrator/src/aspire_orchestrator/routes/trust_hub.py`
- Tests: `backend/orchestrator/tests/test_trust_hub_routes.py`
- Router registered in: `server.py` line ~295

## Reused helpers from front_desk.py
- `_resolve_scope(x_tenant_id, x_suite_id, x_office_id)` — raises 401/422 on missing/invalid headers
- `_validate_cap_token(cap_token, scope, required_scope)` — raises 401 on missing/invalid/expired/wrong-scope token
- `_cap_token_id(cap_token)` — extracts deterministic UUID for receipt tracing
- Import via: `from aspire_orchestrator.routes.front_desk import _cap_token_id, _resolve_scope, _validate_cap_token`

## Vault (Supabase pgsodium) RPC convention
- Create: `supabase_rpc("create_vault_secret", {"secret": value, "name": name, "description": "..."})`
  - Returns `{"id": "uuid"}` — extract via `result.get("id") or result.get("secret_id") or result.get("uuid")`
  - If none of those keys present → raise SupabaseClientError (fail-closed, 503 to caller)
- Delete: `supabase_rpc("delete_vault_secret", {"secret_id": str(uuid)})`
  - Best-effort: wrap in try/except, log warning, do NOT raise
- Vault secret name MUST be `{tenant_id}:{field_type}` to prevent cross-tenant collision (W1 R-004)
- On re-submit/dispute: delete OLD vault secret BEFORE creating new one (W1 R-005)

## ARQ enqueue from FastAPI route
- Pattern: create arq pool inline (no persistent pool on the web process)
- `from arq.connections import RedisSettings, create_pool`
- `pool = await create_pool(RedisSettings.from_dsn(redis_url))`
- `await pool.enqueue_job("advance_trust_state", trust_profile_id, _queue_name="arq:trust_onboarding", _job_id=dedup_key)`
- `await pool.aclose()`
- Entire call is best-effort in a try/except — W9 cron recovers if Redis is down

## Status-callback webhook (W5 — full dispatch)
- Always return 200 to Twilio even on internal errors (Twilio retries on non-2xx)
- Parse form body: `form = dict(await request.form())` — requires python-multipart installed
- Validate HMAC before ANY DB reads: `verify_twilio_signature(request_url=..., form_params=..., signature_header=...)`
- Look up profile by iterating SID columns; HOIST `matched_sid_column` out of the for-loop (critical — W3 skeleton lost it)
- Bundle type mapped from column name: `twilio_secondary_profile_sid` → "profile", `twilio_shaken_bundle_sid` → "shaken", `twilio_cnam_bundle_sid` → "cnam"
- State advancement map: profile+approved→profile_approved, profile+rejected→profile_rejected, shaken+approved→shaken_approved, shaken+rejected→failed, cnam+approved→cnam_approved, cnam+rejected→failed
- Idempotency check: use `_STATE_RANK` dict to determine forward progress; skip if current_rank >= target_rank (and new_state != "failed")
- Rejection payload: `FailureReason` and `ErrorCode` from form fields → stored in `rejection_reason` / `rejection_code` columns (service-role-only) — NOT in redacted_inputs (PII risk)
- Timestamps: set `profile_approved_at` on profile approval, `cnam_approved_at` on CNAM approval
- ARQ enqueue ONLY on approval events — never on rejection (tenant must dispute first)
- Error fallback: catch all exceptions, cut `webhook_processing_failed` receipt (added to RECEIPT_TYPES in trust_receipts.py), return 200
- `webhook_processing_failed` receipt type was added to `trust_receipts.py:RECEIPT_TYPES` in Wave 5

## Test isolation: pytest class-based tests with _PatchCtx
- CRITICAL BUG: `{**VALID_KYB_BODY}` is a shallow copy — `authorized_reps` list is shared
  - Mutating `body["authorized_reps"][0]` contaminates the module-level VALID_KYB_BODY
  - Fix: always call `_valid_kyb_body()` (returns deep copy) for tests that mutate authorized_reps
  - Symptom: subsequent tests that use VALID_KYB_BODY get 422 (Pydantic rejects mutated dob/ssn)
- _PatchCtx start/stop pattern: all defaults in `start()`, overrides replace specific targets
  - Each test class `setup_method` builds fresh `FastAPI` + `TestClient`
  - Patches are applied to module namespace, not to the app instance
  - Call `ctx.stop()` in `finally` to prevent patch bleed

## PII receipt enforcement (Law #9)
- `cut_trust_receipt` raises `TrustReceiptError("PII_LEAK_BLOCKED", ...)` if any of these keys appear in redacted_inputs/redacted_outputs:
  - `email, phone_e164, phone_number, first_name, last_name, full_name, dob, date_of_birth, ssn, ssn_last4, ein, tax_id, address_street, raw_business_name, owner_name`
- Safe keys in redacted_inputs: `trust_profile_id, step_name, rep_count, business_type, address_state, vault_secret_count, dispute_count`
- Route catches `TrustReceiptError` → returns 500 with `{"error": "RECEIPT_FAILED", "reason_code": exc.code}`

## trust_state_transitions constraint
- Table has `CONSTRAINT tst_no_self_loop CHECK (from_state != to_state)`
- For the webhook_received skeleton receipt (W3), use `from_state="webhook_pending"` → `to_state=current_state`

## Dispute route caps
- Route-layer cap: `dispute_count >= 5` → 409 `MAX_DISPUTES_REACHED`
- DB allows up to 10 (migration 113 CHECK constraint)
- This is intentional: route is more conservative than DB

## supabase_select filters
- Format: `f"{column}=eq.{value}"` for single filters
- Multiple: `f"col1=eq.{v1}&col2=eq.{v2}"`
- The `supabase_select` signature: `(table, filters, *, order_by=None, limit=None)`
