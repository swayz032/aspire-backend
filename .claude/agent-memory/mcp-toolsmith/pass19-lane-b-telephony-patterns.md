---
name: Pass 19 Lane B Telephony Patterns
description: Twilio TollFree search, Lookup v2, forwarding instructions, A2P gate, Sarah §3.5 full payload, caller-ID lookup priority, migration 106 RLS pattern, provider_secret_registry env-branching
type: project
---

## Twilio Search Patterns

- TollFree hits `/US/TollFree.json`, must NOT include `AreaCode` in params (non-geographic). Monthly cost = 200 cents ($2.00).
- Local hits `/US/Local.json` with `AreaCode`. Monthly cost = 100 cents ($1.00).
- `CircuitOpenError` constructor: `(breaker_name: str, opened_for_seconds: float)` — NOT a single string arg.

## Twilio Lookup v2

- URL: `https://lookups.twilio.com/v2/PhoneNumbers/{phone}?Fields=line_type_intelligence`
- Returns `line_type_intelligence.carrier_name`, `.type`, plus full dict.
- 404 = number not found → return None (not an error).
- `CarrierInfo` Pydantic model. Never log full phone number — only prefix.

## Forwarding Instructions Service

- AT&T CCF: `**21*{target}#` (always), `**61*{target}#` (no-ans), `**67*{target}#` (busy), `**62*{target}#` (unreachable)
- Verizon: `*72{target}` (always), `*71{target}` (busy-no-ans)
- T-Mobile: `**21*{target}#` (always), `**61*{target}#` (conditional)
- Generic fallback: all 4 AT&T patterns
- Carrier matching is case-insensitive via `_normalise()` lowercasing.

## A2P Gate in sms_io.send_sms

- Add gate BEFORE `from_number` resolution to minimise unnecessary DB calls.
- Filter: `tenant_id=eq.{tenant_id}` — must use scope.tenant_id, never from payload.
- Service-layer cross-tenant check: compare `row["tenant_id"]` to `scope.tenant_id`. Deny if mismatch (defence in depth vs RLS bypass).
- Receipt on block: `receipt_type="sms_send_blocked_a2p"`, `outcome="denied"`, `reason_code="a2p_not_registered"`.
- Block any status that is not "registered" — including "pending_brand", "pending_campaign".

## Migration 106 RLS Pattern

- `update_updated_at_column()` is the correct trigger function name (not `moddatetime()` from extensions schema).
- `tenant_memberships.tenant_id` is TEXT not UUID — don't use JOIN-based policies.
- Use JWT claim pattern: `tenant_id::text = current_setting('request.jwt.claim.tenant_id', true)`.
- Always use `DROP POLICY IF EXISTS` before `CREATE POLICY` for idempotency.

## Sarah §3.5 Full Payload (Pass 19)

New fields added to dynamic_variables:
- `is_after_hours`: bool — inverse of `is_open_now`
- `tenant_id`: str — scope identifier
- `office_id`: str — scope identifier
- `voicemail_email`: str — from `office_profiles.voicemail_email`
- `caller_history_summary`: "" (V1 empty, V2 will be prior call digest)
- `public_number_mode` default updated from `ASPIRE_NUMBER` to `ASPIRE_NEW_NUMBER`

`_fetch_profile` now returns 6-tuple: `(biz_name, first_name, last_name, industry, tz_name, voicemail_email)`.

## HMAC Bypass

- `settings.disable_personalization_hmac` + `settings.aspire_env != 'prod'` = bypass OK in dev.
- When mocking `settings` with `MagicMock(elevenlabs_webhook_secret=...)`, ALWAYS also set `disable_personalization_hmac=False` and `aspire_env="dev"` — otherwise MagicMock returns truthy for all attributes and the bypass fires unexpectedly.

## Cache Invalidation (§3.5.5)

- `invalidate_personalization_cache_for_office(office_id)` exported from `routes/sarah.py`.
- Called in `routes/front_desk.py` PATCH handler after successful versioned write.
- Non-fatal: wrapped in try/except, logs warning on failure.
- In-process LKG cache only (V1). Redis invalidation deferred to V2.

## Caller-ID Lookup Route (`/v1/calls/caller-id-lookup`)

- Priority: routing_contacts (phone match, office-scoped) → sms_thread memory → call memory (last 90 days) → unknown fallback.
- Capability token required; parse from X-Aspire-Capability-Token header as JSON string.
- `validate_token` takes a dict (not tuple) — parse JSON, fall back to `{}` on error (never None).
- `validate_token` returns a `TokenValidationResult` object — check `.valid`, `.error.value`.
- Receipt on every call including fallback. Law #9: phone_prefix only in receipts.
- Registered in server.py: `app.include_router(calls_router)  # /v1/calls/*`

## Provider Secret Registry Env-Branching

- Added `secret_id_by_env: {dev, staging, prod}` map to each entry.
- `get_secret_id_for_env(meta, aspire_env)` helper in `services/provider_secret_registry.py`.
- `secrets.py` load_secrets: ASPIRE_ENV `production|prod|staging` → uses `aspire/prod/*` paths.
- Added `aspire/prod/elevenlabs` as a separate granular group (optional on dev, non-critical).
- Settings: added `aspire_env: str = "dev"` and `disable_personalization_hmac: bool = False`.
