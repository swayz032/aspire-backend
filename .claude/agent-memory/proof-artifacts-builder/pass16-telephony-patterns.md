---
name: Pass 16 Telephony Runbook Patterns
description: Receipt types, DB tables, idempotency gaps, EL API contract, file paths for Pass 16 telephony/SMS runbooks
type: project
---

## Receipt types (Pass 16 telephony)
- `phone_number_purchase` / `phone_number_purchase_failed` — twilio_provisioning.py
- `phone_number_release` — twilio_provisioning.py
- `personalization_resolve` — routes/sarah.py (success path)
- `personalization_denied` — routes/sarah.py (INVALID_SIGNATURE, MISSING_WEBHOOK_SECRET, INVALID_CALLED_NUMBER)
- `personalization_unknown_number` — routes/sarah.py (404 path)
- `sms_outbound` — sms_io.py
- `sms_status_update` — sms_io.py (terminal statuses only: delivered/failed/undelivered)
- Inbound SMS receipts come from MemoryService.write() path, NOT from SMSIngestionAdapter directly

## Key DB tables (migration 102)
- `public.tenant_phone_numbers` — twilio_sid, elevenlabs_phone_number_id, status (reserved/active/released)
- `public.front_desk_configs` — after_hours_mode, busy_mode, public_number_mode, catch_mode; versioned (is_current=true)
- `public.front_desk_routing_contacts` — role + phone for owner/sales/support/billing/scheduling
- `public.sms_messages` — direction, status, error_code, idempotency_key, message_sid

## Idempotency gaps (known, Pass 18 items)
- `_idem_store` in twilio_provisioning.py is IN-MEMORY — cleared on pod restart. Phase 2 ships Redis/Supabase persistent store. Workaround: query DB by suite_id + office_id before retrying.
- SMS send idempotency is minute-bucketed SHA256 on client side — no server-side dedup in DB.

## EL API contract (verified 2026-04-29)
- POST /v1/convai/phone-numbers — import Twilio number (provider='twilio', sid=workspace_SID not number_SID)
- PATCH /v1/convai/phone-numbers/{id} — attach to agent_id
- DELETE /v1/convai/phone-numbers/{id} — detach + remove; 404 = already gone (idempotent)
- GET /v1/convai/phone-numbers — list; response may be list or {phone_numbers: []}
- supports_inbound/supports_outbound are DEPRECATED — do not send
- Sarah Receptionist agent ID: agent_6501kp71h69jfqysgd055hemqhrq (stable, verified 2026-04-29)
- Personalization webhook MUST return ALL 16 custom variables — missing keys break the agent for that call

## Pass 18 security fixes already in source (reference for evil test coverage)
- THREAT-014: E.164 regex validation on called_number before PostgREST filter build (sarah.py:249)
- THREAT-015: scope binding on release_number — suite_id + office_id filter prevents cross-tenant release (twilio_provisioning.py:461)
- THREAT-017: PII masking on phone numbers in receipts — first 6 digits only (sms_io.py:244)
- Fail-closed on empty ELEVENLABS_WEBHOOK_SECRET (sarah.py:191) — prior bypass was a Law #3 violation

## Operational gap: Railway env promotion clears production-only secrets
- Staging does not have ELEVENLABS_WEBHOOK_SECRET — Railway promote-to-prod clears it
- Postmortem example documents this as a recurring risk (SEV2 incident scenario)
- Action item for any future runbook: note which env vars are production-only and must be re-verified after Railway env operations

**Why:** Discovered while writing postmortem-template.md example — the fail-closed fix in sarah.py directly addresses this operational gap.
**How to apply:** When reviewing changes to sarah.py or writing test evidence for telephony PRs, always verify ELEVENLABS_WEBHOOK_SECRET behavior is tested under three conditions: set+valid, set+invalid, unset.
