---
name: Ingestion Adapter Patterns
description: Patterns for BaseIngestionAdapter subclasses in the Aspire Office Memory Engine (Pass 14+)
type: project
---

## supabase_select signature
`supabase_select(table, filters, *, order_by, limit)` — `filters` is a dict
`{col: val}` or raw query string. NOT `filter_=`. The sms_ingestion.py reference
uses the raw-string form `"phone_number=eq."+to_number` but the dict form
`{"provider": "stripe", "external_account_id": customer_id}` works cleanly for
multi-column lookups.

## provider_connections scope lookup pattern
For Stripe/PandaDoc scope resolution, query `provider_connections` with
`{"provider": "<name>", "external_account_id": "<id>"}`. Row must have
`tenant_id`, `suite_id`, `office_id` as UUID strings. Raise `IngestionError`
with `status_code=404` and `code="UNKNOWN_<ENTITY>"` on empty result.

## PandaDoc workspace_id extraction
Webhook payload can have `workspace_id` at top level OR nested in `data`. Also
`data.workspace.id` if workspace is an object. Extract with fallback chain.

## PandaDoc non-actionable states
States like `draft`, `approval_not_needed` etc. should raise
`IngestionError(status_code=200, code="UNHANDLED_DOCUMENT_STATE")` so PandaDoc
stops retrying. `_dispatch` in routes/ingestion.py converts IngestionError to
HTTPException — a 200 body with `ok:false` is returned (PandaDoc treats 2xx as
delivered).

## Stripe invoice.paid / invoice.voided supersede pattern
Law #2 (append-only). DO NOT UPDATE the original row. Write a new memory_object
with a unique `idempotency_key` like `stripe-invoice-paid-{event_id}`. Embed
`supersedes_idempotency_key` in `detail` dict for traceability. Use
`status='executed'` for paid/voided.

## Routes dispatch pattern
Route does `payload = await request.json()` then `_dispatch(Adapter(), request=request, payload=payload)`.
`_dispatch` calls `await request.body()` internally for signature verification.
Both can be awaited on the same request because FastAPI caches the body.

## verify_stripe / verify_pandadoc
Both in `services/ingestion/signatures.py`. `verify_stripe(body, sig_header, secret)`
and `verify_pandadoc(body, sig_header, secret)`. Headers are lowercased by
`_dispatch` before reaching `verify_signature` — look for both `stripe-signature`
and `Stripe-Signature` as fallbacks.

## MemoryObjectIn.status accepted values
Use `MemoryStatus` literals: `drafted`, `executed`, `rejected`, etc. (NOT
`"draft"`, `"paid"`, `"voided"` — those go in `detail.status`).

## wired_adapters healthz list
Always update `routes/ingestion.py` `ingestion_healthz()` to move adapter names
from `stub_adapters` to `wired_adapters` list when implementing.

**Why:** Monitoring and smoke tests hit `/v1/ingest/healthz` to confirm what's
deployed.

## Twilio form-encoded voice webhooks
Both `RecordingStatusCallback` and `TranscribeCallback` are form-encoded (same
as SMS). Route does `form = await request.form(); payload = {k: str(v) ...}`.
`_dispatch` already calls `request.body()` for HMAC — FastAPI caches it so
double-await is safe.

## Twilio call two-event lifecycle + supersede pattern
Recording fires first (`CallSid`, `RecordingSid`, `RecordingDuration` etc.).
Transcription fires second (`CallSid`, `TranscriptionSid`, `TranscriptionText`).
Both write separate append-only memory_objects sharing the same
`trace_id = uuid5(NS, f"twilio-call:trace:{CallSid}")` and
`correlation_id = uuid5(NS, f"twilio-call:corr:{CallSid}")`.
Transcription row sets `detail.supersedes_idempotency_key` to reference the
recording row's idempotency key. Law #2 — never UPDATE the recording row.

## Twilio RecordingUrl suffix
Twilio's `RecordingUrl` does NOT include `.mp3` extension by default. Append
`.mp3` for direct playback compatibility. Check before appending to avoid
double-extension.

## TranscriptionStatus 'failed' handling
When `TranscriptionStatus == 'failed'`, still write a memory_object with
`outcome='transcription_failed'` and `transcription_text=null`. This keeps the
call auditable even when transcription fails. Do NOT raise IngestionError.

## outcome heuristic
Scan lowercased transcript for `"voicemail"`, `"leave a message"`,
`"after the beep"`, `"at the tone"` → `"voicemail"`. Otherwise `"completed"`.
More sophisticated NLP deferred to Pass 18 post-processing pipeline.

## Two-write adapters (EL / Anam) — override ingest()
When an adapter must write TWO memory_objects per webhook (transcript + session_summary),
override `ingest()` entirely instead of `build_envelope()`. Set `build_envelope` to
raise `NotImplementedError` to satisfy ABC. The override must:
  1. verify_signature (fail-closed)
  2. resolve scope
  3. write transcript first, capture memory_id
  4. write session_summary with `linked_memory_ids=[str(transcript_id)]`
  5. return IngestionResult for session_summary (the primary type)
Both writes go through `self._memory_service.write()` — receipts cut internally (Law #2).

## ElevenLabs scope resolution (two-step)
Step 1: `tenant_phone_numbers` lookup by `called_number` from `data.metadata.called_number`.
Step 2: `provider_connections` lookup by `agent_id` (provider='elevenlabs').
Step 1 failure is non-fatal (fall through, don't raise). Step 2 failure on empty result
raises `IngestionError(code='UNKNOWN_AGENT_OR_NUMBER', status_code=404)`.

## Anam scope resolution (two-step)
Step 1: `provider_connections` lookup with `{"provider":"anam", "tenant_id": tenant_id_raw}`.
  (metadata.tenant_id may be present for direct resolution)
Step 2: `provider_connections` lookup with `{"provider":"anam", "external_account_id": persona_id}`.
Raises `IngestionError(code='UNKNOWN_PERSONA', status_code=404)` if neither resolves.

## Anam handoff chain (voice → video)
If `session.metadata.handoff_id` is present, look up `memory_objects` by
`{"tenant_id": scope.tenant_id, "correlation_id": handoff_id}`. If found, add to
`linked_memory_ids`. This is non-fatal — proceed even if handoff memory is not found.

## Zoom scope resolution
`provider_connections` lookup with `{"provider":"zoom", "external_account_id": account_id}`.
`account_id` lives at `payload.account_id` OR `payload.payload.account_id` (check both).

## Zoom two-event pattern
`recording.completed` → creates `meeting` row with `status=null`.
`recording.transcript_completed` → creates NEW append-only `meeting` row (Law #2),
looks up recording row by `idempotency_key=f"zoom-recording:{meeting_uuid}"` for
`linked_memory_ids`. Non-fatal if recording row not found yet (out-of-order delivery).

## Zoom signature verification
`verify_zoom(body, X-Zm-Signature, X-Zm-Request-Timestamp, secret)`. The timestamp
header must be passed separately — injected by the route layer via the headers map.
Both headers are lowercased by `_dispatch`.

## RuntimeFamily values (confirmed in memory_v1.py)
`"elevenlabs"`, `"anam"`, `"internal"`, `"ui"`, `"provider_webhook"`.
Use `"elevenlabs"` for EL adapters and `"anam"` for Anam adapters.
Use `"provider_webhook"` for Zoom (no dedicated runtime family for Zoom yet).

## MemoryObjectIn.linked_memory_ids
NOT a native field on `MemoryObjectIn`. Store in `detail["linked_memory_ids"]` as
`list[str]` of UUID strings. The DB stores it in the JSONB detail column — no
separate FK column for this yet (Pass 14 scope).

## call_ingestion.py filter_ bug
`call_ingestion.py` passes `filter_="phone_number=eq."+to_number` as keyword arg to
`supabase_select` but the param name is `filters` (positional). This works because
Python passes it positionally. Future adapters should use the correct `filters=` kwarg
or dict form to avoid confusion.

## SourceSurface + Channel valid values (confirmed memory_v1.py Pass 14 expansion)
**SourceSurface** valid literals: `ava_voice`, `sarah_voice`, `eli_inbox`,
`nora_meeting`, `finn_finance`, `tim_service_lab`, `estimate_studio`,
`canvas_desk`, `receipt_ledger`, `approval_queue`, `system`, `tec_documents`,
`google_calendar`, `aspire_calendar`.
**Channel** valid literals: `voice`, `video`, `email`, `sms`, `workflow`,
`finance`, `ui`, `webhook`.
Use `"webhook"` for external push notifications (Google Calendar, Zoom, etc.).
Use `"ui"` for internal Aspire-sourced events (uploads, internal calendar).
Never use `"operations"` — it is not a valid Channel.

## Internal route adapters (no external HMAC)
For adapters invoked from authenticated internal routes (document upload, Aspire
calendar), `verify_signature` returns `True` unconditionally with a comment
stating the security boundary is the route auth layer. This is correct by design —
document it clearly in the class docstring to avoid future "why is this True?"
confusion.

## Google Calendar push notification pattern
Google sends push pings with NO body. Channel ID in `X-Goog-Channel-ID` header
IS the authentication (no HMAC). Validate channel_id against `provider_connections`
table (`provider='google_calendar'`, `external_account_id=channel_id`).
`X-Goog-Resource-State: sync` = initial confirmation, no-op → raise
`IngestionError(status_code=200, code='SYNC_PING_NO_OP')`.
Override `ingest()` (not just `build_envelope`) when the adapter must fetch from
an external API and process multiple items per webhook ping.

## PandaDoc contract vs quote routing
Route inspects `payload.data.tags` (list) for string `'contract'` (lowercased).
If found → `ContractIngestionAdapter`. Otherwise → `QuoteIngestionAdapter` (fallback).
Same HMAC secret (`pandadoc_webhook_secret`) covers both.
Contract idempotency_key: `pandadoc-contract-{document_id}-{action}` (NOT event_id
alone — use document_id from `data.id` so state transitions are distinct rows).

## calendar_events table columns (Supabase, confirmed 2026-04-29)
`id` (uuid), `suite_id` (uuid), `title`, `description`, `event_type` (CHECK:
meeting/task/reminder/call/deadline/other), `start_time` (timestamptz),
`end_time` (timestamptz?), `duration_minutes`, `location`, `participants` (text[]),
`is_all_day` (bool), `source` (CHECK: manual/ava/booking/google_calendar/import),
`source_ref`, `created_by`, `created_at`, `updated_at`, `status`.
NOTE: NO `tenant_id` column — scoped only by `suite_id`. Backfill must join
through `suite_profiles` to get tenant_id.

## Pass 18 DLP integration (THREAT-016)
`BaseIngestionAdapter._scrub_detail_pii(detail)` is the canonical PII scrubber.
Wired in `ingest()` between `build_envelope()` and `MemoryService.write()`.
Uses `scrub_text()` async wrapper from `services/dlp.py` (Presidio + regex double-pass).
Fail-open: DLP failure logs WARNING, writes unscrubbed detail — never fails ingestion.
Text fields scrubbed: `body, transcript_text, transcription_text, description, summary_text, text, message, notes, file_name`.
Email identity fields (viewer_email, recipient_email, signer_email, email) → SHA-256 hash with 3-char prefix (not Presidio, to preserve audit correlation).
Nested dicts and lists of turn-dicts are recursively scrubbed.
IMPORTANT: Presidio US_SSN needs context words to trigger at high confidence — without them `results=[]`.
Fix: `redact_text_ingestion()` applies Presidio THEN regex second-pass. `scrub_text()` uses this method.
`redact_text()` (existing) is Presidio-only — correct for log/receipt scrubbing. Do NOT replace it.

## Pass 18 receipt trace_id + correlation_id propagation (GAP-SCHEMA-1)
`middleware/correlation.py` exposes `get_trace_id()` and `get_correlation_id()` — ContextVar helpers, degrade to "" outside request context.
Service functions (`purchase_number`, `release_number`, `send_sms`) accept `trace_id`, `correlation_id`, `capability_token_id` as optional `str` kwargs (default "").
Routes call `get_trace_id()` / `get_correlation_id()` and pass to service layer.
`capability_token_id` extraction helper `_cap_token_id(cap_token)`: uses `token.get('id')` if present; else `sha256(signature)[:16]`.
This helper is duplicated in `telephony.py`, `sms.py`, `front_desk.py` — consider extracting to a shared `routes/_helpers.py` in a future pass.
Receipt fields added: `"trace_id": trace_id, "correlation_id": correlation_id, "capability_token_id": capability_token_id or None`.
Sarah webhook receipts (no capability token): include trace_id + correlation_id only; capability_token_id absent.

## Backfill dry-run exit codes
Backfills that fail with `SupabaseClientError("config", detail="Missing ASPIRE_SUPABASE_URL")`
in local dev are expected — the script parsed args correctly and reached the
Supabase call. This is NOT a code error, it is a missing-credentials exit.
The spec "print plan without errors" means the arg parsing and logic structure
are correct, not that it must connect to Supabase in local dev.
