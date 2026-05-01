---
name: Pass 16 Telephony + Front Desk Patterns
description: Service/route patterns for Twilio provisioning, EL phone import, SMS IO, and Front Desk config introduced in Pass 16
type: project
---

# Pass 16 Telephony + Front Desk Patterns

## Key Constants
- `SARAH_RECEPTIONIST_AGENT_ID = "agent_6501kp71h69jfqysgd055hemqhrq"` — verified 2026-04-29
- `supabase_select` signature: `(table, filters: str | dict, *, order_by=None, limit=None)`
  - `filters` as string: `"column=eq.value&column2=eq.value2"` — pass raw PostgREST filter string
  - `filters` as dict: `{"column": "value"}` — auto-converted to `column=eq.value`

## ElevenLabs Phone API (verified 2026-04-29)
- Import: `POST /v1/convai/phone-numbers` body: `{phone_number, label, provider:'twilio', sid, token}`
- Attach: `PATCH /v1/convai/phone-numbers/{id}` body: `{agent_id}`
- Delete: `DELETE /v1/convai/phone-numbers/{id}`
- `supports_inbound` / `supports_outbound` DEPRECATED — do NOT send
- Auth header: `xi-api-key: <key>` (NOT `Authorization: Bearer`)
- Idempotency on POST: EL returns 409 if already imported; GET list and find by phone_number

## Twilio Provisioning Patterns
- Purchase: `POST /2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json` (form-encoded, auth=(sid,token))
- Search: `GET /2010-04-01/Accounts/{sid}/AvailablePhoneNumbers/US/Local.json`
- Release: `DELETE /2010-04-01/Accounts/{sid}/IncomingPhoneNumbers/{incoming_sid}.json`
- `voice_url` left blank on purchase — EL auto-writes it on import (verified behavior)
- `sms_url` must be set to Aspire backend (EL is voice-only, SMS is entirely Aspire ↔ Twilio)

## Route Token Validation Pattern
All Yellow-tier routes follow this pattern:
```python
scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
_validate_cap_token(req.capability_token, scope, "scope:action")
```
`validate_token()` from `token_service.py` takes: `(token_dict, *, expected_suite_id, expected_office_id, required_scope)`

## Personalization Webhook (Sarah)
- EL inbound payload: `{caller_id, agent_id, called_number, call_sid}`
- Response MUST include ALL custom dynamic_variables defined on the agent (missing = agent broken)
- `system__` prefixed vars are EL-injected — do NOT send them
- `conversation_config_override` is optional, used for dynamic first_message
- Response type: `"conversation_initiation_client_data"` (exact string)
- Table dependency: `tenant_profiles` (business_name, industry), `office_profiles` (first_name, last_name, timezone), `business_hours` (day_of_week 0=Mon, open_time, close_time)

## Front Desk Config Versioning
- PATCH inserts NEW row with `version_no = max+1` — never UPDATE existing rows (Law #2)
- `is_current` trigger in migration 102 handles marking max version current
- Soft-delete routing contacts: set `is_active=False` (no hard DELETE per Law #2)

## SMS IO Architecture
- EL is VOICE-ONLY — SMS entirely Aspire ↔ Twilio
- Outbound: resolve from_number via `tenant_phone_numbers WHERE office_id AND sms_enabled AND status='active'`
- Resolve to_number: `memory_objects WHERE memory_id=thread_id → detail.from`
- Append-only memory: new `memory_objects` row with `linked_memory_ids=[thread_memory_id]` (never mutate thread row)
- Status callback terminal states: `delivered`, `failed`, `undelivered` → cut receipt

## DB Tables Assumed (migration 102)
- `tenant_phone_numbers`: id, tenant_id, suite_id, office_id, phone_number, twilio_sid, elevenlabs_phone_number_id, attached_to_agent_id, status, sms_enabled, voice_enabled, monthly_cost_cents, purchased_at, released_at
- `front_desk_configs`: id, tenant_id, suite_id, office_id, version_no, is_current, public_number_mode, catch_mode, after_hours_mode, busy_mode, greeting_name_override, pronunciation_override, last_forwarding_test_at, last_forwarding_test_result, forwarding_status
- `front_desk_routing_contacts`: id, tenant_id, suite_id, office_id, role, label, phone, sip_uri, email, is_active
- `sms_messages`: id, tenant_id, suite_id, office_id, thread_memory_id, message_sid, direction, from_number, to_number, status, error_code, idempotency_key, sent_at, updated_at
- `tenant_profiles`: tenant_id, business_name, industry (onboarding table — may be absent, fallback gracefully)
- `office_profiles`: office_id, first_name, last_name, timezone (onboarding table — may be absent)
- `business_hours`: office_id, day_of_week (0=Mon..6=Sun), open_time, close_time

## Why:
Pass 16 wires Sarah Receptionist end-to-end: phone purchase → EL import → personalization webhook → routing. The EL voice path is entirely EL-managed; only SMS is Aspire-managed.
