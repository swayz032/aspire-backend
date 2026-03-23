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
