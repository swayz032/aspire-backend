# Receipt Ledger Auditor — Agent Memory

## Key Files (Audit Infrastructure)
- `services/receipt_store.py` — dual-write (in-memory + async Supabase); `store_receipts` (non-blocking) and `store_receipts_strict` (YELLOW/RED fail-closed)
- `services/receipt_chain.py` — SHA-256 hash chain, `assign_chain_metadata`, `verify_chain`
- `services/receipt_schema_registry.py` — JSON schema validation; default mode is "warn" (not "strict")
- `nodes/approval_check.py` — ALL approval outcomes covered (green auto, yellow/red request, denied, binding fail, presence fail, granted)
- `nodes/execute.py` — ALL execution paths covered (safe-mode, token denied, idempotency dup, outbox, success, failure, timeout)
- `nodes/resume.py` — ALL validation paths covered; error helper `_error()` uses `ReceiptType.TOOL_EXECUTION` for denial receipts (schema mismatch — see patterns.md)

## Confirmed Patterns
- State machines (invoice, payment, contract, payroll, mail) are pure logic — receipts in TransitionReceipt dataclass, persisted by callers. Confirm callers persist via receipt_store.
- `supabase_update` is used on NON-receipt tables only: `approval_requests` (status→executed), `agent_semantic_memory` (fact upsert), `contracts` (state sync), `inbox_items` (message status), `workflow_executions`. None of these are the `receipts` table. Law #2 is not violated.
- Receipt store has NO delete/update on receipts table. Only INSERT with ON CONFLICT DO NOTHING.

## Known Schema Gaps (Cycle 5 findings — see patterns.md for detail)
- 18 required fields; most service-specific receipt emitters (backup, deployment, rbac, entitlement) use abbreviated schemas missing: `actor_type` enum (uses "system" string), `capability_token_id`, `capability_token_hash`, `idempotency_key`, `redacted_inputs`, `redacted_outputs`, `receipt_type` (some missing)
- episodic_memory and semantic_memory receipts use `receipt_id` instead of `id`, and have non-standard field names
- feature_flags receipt uses `risk_tier: "GREEN"` (uppercase) — may fail DB enum check
- resume.py `_error()` uses `ReceiptType.TOOL_EXECUTION` for denial receipts — should be `ReceiptType.VALIDATION_FAILED` or similar
- `store_receipts_strict` called-from-event-loop-thread edge case: schedules flush but does not await — creates a non-deterministic window where receipt may not be persisted before response

## Pass 18 Audit Findings (2026-04-29) — Office Memory Engine Ship Gate
- All 13 ingestion adapters delegate receipts via MemoryService.write — NO adapter calls store_receipts directly. Receipt cut is centralized in memory_service.py:412. Idempotency dedup path (lines 375-385) confirmed to NOT re-emit receipt (correct).
- ALL ingestion adapters have failure receipts via IngestionError bubbling through base.ingest → HTTPException. BUT: the route layer (_dispatch) converts IngestionError to HTTPException without cutting a *_failed receipt. This means ingest failures (bad signature, scope fail, envelope build fail) are SILENT — no failure receipt in the receipts table.
- twilio_provisioning.py: purchase_number cuts phone_number_purchase_failed receipt on rollback (line 318) and phone_number_purchase on success (line 335). release_number cuts phone_number_release on success only — no failure receipt if Twilio DELETE or EL detach fails.
- sms_io.py: send_sms cuts sms_outbound receipt (line 238). update_sms_status cuts sms_status_update only on terminal states (correct per Law #2). No failure receipt if Twilio POST fails in send_sms.
- routes/sarah.py: personalization_denied cut on invalid signature (line 183), personalization_unknown_number on 404 (line 223), personalization_resolve on success (line 315). No receipt on JSON parse error (lines 200-205) or generic DB error paths.
- routes/front_desk.py: all 5 write operations (patch_config, test_call, create_contact, update_contact, delete_contact) cut receipts. No failure receipts for DB errors in patch_config/create_contact/update_contact.
- CRITICAL SCHEMA GAP — All telephony/sms/sarah/front_desk receipts missing: trace_id, correlation_id, actor_id, capability_token_id, idempotency_key. These are orphaned (not linkable to requesting operation).
- PII RISK: sms_io.py line 260 logs from_number, to_number, message body (first 80 chars) at INFO. Phone numbers are PII (Law #9).
- SECRET RISK: routes/ingestion.py twilio_sms_status route (line 188) references `settings` but never imports it — NameError at runtime on status callbacks.
- calendar_ingestion.py GoogleCalendarIngestionAdapter: `access_token` read from provider_connections row at line 611. If the row has `credentials` JSONB, the nested access_token is not logged (correct). But if row.get("access_token") returns the token directly, it is logged at warning level (line 616 — only a warning that it's missing, not the value itself). Confirmed safe.
