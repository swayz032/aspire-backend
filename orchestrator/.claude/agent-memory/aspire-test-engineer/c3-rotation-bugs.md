---
name: C3 Rotation Code Scan Bugs
description: All bugs found in C3 rotation scan (server.py, settings.py, middleware/, services/, routes/)
type: project
---

# C3 Rotation Scan Bugs (2026-03-23)

Scan covered: server.py, config/settings.py, middleware/*.py, services/token_service.py,
services/approval_service.py, services/receipt_store.py, services/idempotency_service.py,
services/policy_engine.py, routes/admin.py, routes/intents.py, routes/webhooks.py,
routes/robots.py, config/secrets.py

**Why:** Full production readiness scan before Gate 1 sign-off.
**How to apply:** Block any ship decision on BLOCKERs; schedule HIGH within next sprint.

## Bug List

### BUG-01 | BLOCKER | config/settings.py:16-17
supabase_url and supabase_service_role_key both default to "".
In dev, _supabase_enabled() returns False, receipts are in-memory only.
If deployed without these env vars, no receipts persist to Supabase — silent data loss.
Production guard exists in server.py startup check, but it only checks _settings_warnings,
not explicitly these two fields. The settings.py comment says "Supabase" but no sentinel value like token_signing_key has.
Fix: Add sentinel values or verify verify_settings_coverage() explicitly catches empty supabase_url.

### BUG-02 | HIGH | config/settings.py:63
s2s_hmac_secret defaults to "". Domain Rail S2S calls signed with an empty HMAC key
will compute a deterministic signature (HMAC of "" key). Any attacker who sends
a request can trivially forge it. Should be "UNCONFIGURED-FAIL-CLOSED" (same pattern as token_signing_key).
Fix: s2s_hmac_secret: str = "UNCONFIGURED-FAIL-CLOSED"

### BUG-03 | HIGH | config/settings.py:102
quickbooks_base_url defaults to "". Comment says "Default: sandbox. Set to prod URL in production."
But empty string is not a valid URL. Any QuickBooks API call will fail with a cryptic URL error
rather than a clear "not configured" message.
Fix: quickbooks_base_url: str = "https://sandbox-quickbooks.api.intuit.com"

### BUG-04 | HIGH | routes/webhooks.py:44-50
_get_stripe_handler() initializes StripeWebhookHandler with secret="" when STRIPE_WEBHOOK_SECRET
is not set. The handler object is then cached and reused. First call with empty secret will
call verify_stripe_signature() which raises WebhookSignatureError("Webhook secret not configured").
This IS caught and returns 401, so it IS fail-closed. However the singleton is initialized with
the empty secret at first call, and the rotation logic on lines 69-71 re-reads env on each request
(good), but uses _stripe_handler._webhook_secret (accessing private attribute) which is fragile.
If the Stripe provider moves to a dataclass, this breaks silently.
Fix: Access webhook secret through a public method, not _webhook_secret.

### BUG-05 | HIGH | routes/intents.py:344
allow_internal_routing = bool(request.headers.get("x-admin-token"))
This checks header PRESENCE only. An attacker who sets X-Admin-Token: garbage gets
allow_internal_routing=True without any JWT validation. The actual admin JWT is only
validated when _require_admin() is called later. The /v1/intents/classify endpoint
does not call _require_admin() at all — it trusts header presence for internal routing.
Fix: Validate the JWT via _require_admin() before setting allow_internal_routing=True.

### BUG-06 | MEDIUM | routes/webhooks.py:162-185
PandaDoc webhook builds a receipt dict (lines 171-184) but only logs it (line 185).
store_receipts() is never called. Receipt is built but immediately discarded.
Violates Law #2.
Fix: Add store_receipts([receipt]) after line 184, before the return statement.

### BUG-07 | MEDIUM | routes/webhooks.py:287-301
Same issue as BUG-06 for Twilio webhook. Receipt built on lines 287-301, only logged
on line 302. store_receipts() never called.
Fix: Add store_receipts([receipt]) after line 301.

### BUG-08 | MEDIUM | routes/admin.py:235
_require_admin() does `import jwt as pyjwt` inside the function. If PyJWT is not installed
(edge case, unlikely but possible in a stripped container), this raises ImportError which is
not caught. The unhandled ImportError propagates up, triggers GlobalExceptionMiddleware,
but the exception handler itself also tries to import from aspire_orchestrator.routes.admin
creating a potential circular import during error handling path.
Fix: Move import to module level or catch ImportError explicitly.

### BUG-09 | MEDIUM | middleware/exception_handler.py:112-128
Exception handler receipt (lines 112-128) is missing the trace_id field.
The receipt_store._map_receipt_to_row() will derive trace_id from correlation_id as fallback,
but the receipt schema requires trace_id as a first-class field. Other receipts (SSE, admin)
set it explicitly. Inconsistency can break receipt chain verification.
Fix: Add "trace_id": correlation_id to the exception receipt dict at line 128.

### BUG-10 | LOW | server.py:254
_verify_environment_parity() is called at module import time (line 254), before load_secrets()
runs (line 237). If AWS Secrets Manager sets ASPIRE_ENV but not NODE_ENV (or vice versa),
the parity check fires before SM secrets are loaded, causing a false-positive SystemExit.
Fix: Call _verify_environment_parity() after load_secrets(), or defer to lifespan startup.

### BUG-11 | LOW | services/receipt_store.py:486
store_receipts_strict() accesses _receipt_writer._loop (private attribute, line 486) and
_receipt_writer._buffer_lock (line 599 in clear_store). Accessing private attributes of
an async writer from synchronous calling code is fragile — any refactor of _AsyncReceiptWriter
silently breaks this.
Fix: Expose a public flush_sync() method on _AsyncReceiptWriter.

### BUG-12 | LOW | services/approval_service.py:170-233
The 7-check order has CHECK 3 (request_id not reused, line 170-181) before CHECK 7
(request_id matches binding, line 221-229). CHECK 3 marks the key in _used_approval_request_ids
before CHECK 7 validates request_id actually matches. If CHECK 7 fails, the key is NOT added
(because CHECK 7 comes after the add on line 233), but the error code for CHECK 7 failure
is ApprovalBindingError.REQUEST_ID_REUSED — same as the duplicate-use error, misleading.
Fix: Rename CHECK 7 error to APPROVAL_REQUEST_ID_MISMATCH for clarity.

### BUG-13 | LOW | services/token_service.py:82-98
_get_signing_key() logs a WARNING when key length < 32 bytes but still returns the weak key.
If the sentinel value "UNCONFIGURED-FAIL-CLOSED" (29 chars) is accidentally set as the actual
signing key, this logs a warning and continues rather than failing closed.
"UNCONFIGURED-FAIL-CLOSED" is 24 chars — it would silently be used as a signing key in production
if accidentally set. The sentinel design relies on it causing HMAC failures when compared, but
the warning is insufficient.
Fix: Add a check: if key == "UNCONFIGURED-FAIL-CLOSED": raise ValueError().
