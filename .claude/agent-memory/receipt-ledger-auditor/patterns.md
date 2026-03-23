# Receipt Patterns & Findings — Aspire Backend

## Cycle 5 Audit Findings

### FINDING-C5-001 (HIGH) — Abbreviated Receipt Schema in Service Emitters
Files: backup_receipts.py, deployment_receipts.py, rbac_receipts.py, entitlement_receipts.py
Problem: `_base_receipt()` in each of these emits receipts missing required fields:
  - Missing: `capability_token_id`, `capability_token_hash`, `idempotency_key`
  - Missing: `redacted_inputs`, `redacted_outputs`
  - `receipt_type` field present only in some
  - `action_type` is set after base creation (correct) but correlation_id is a NEW uuid4() per call — does not inherit from the triggering operation's correlation_id
Impact: Ops receipts are not traceable back to the triggering request. Chain integrity gaps for GREEN-tier ops.
Fix needed: Accept `correlation_id` as a parameter (not generate new), add stub fields for `capability_token_id=None`, `redacted_inputs={}`, `redacted_outputs={}`.

### FINDING-C5-002 (HIGH) — Episodic/Semantic Memory Receipt Field Names
Files: episodic_memory.py (line 160), semantic_memory.py (line 188)
Problem: Receipts use `receipt_id` (not `id`) and `ts` (not `created_at`). The receipt_store `_map_receipt_to_row()` looks for `id` field and `created_at`. With `receipt_id` instead of `id`, the mapper generates a new UUID, losing the receipt_id from the record.
Fix needed: Rename `receipt_id` → `id`, `ts` → `created_at`. Add `office_id` (missing entirely in episodic receipt). Add `actor_id` (uses `actor: "service:episodic-memory"` not `actor_id`).

### FINDING-C5-003 (MEDIUM) — feature_flags Risk Tier Case Mismatch
File: feature_flags.py (line 72)
Problem: `"risk_tier": "GREEN"` uses uppercase. The DB enum and all other emitters use lowercase (`"green"`). May fail DB column constraint depending on CHECK definition.
Fix needed: Change to `"risk_tier": "green"`.

### FINDING-C5-004 (MEDIUM) — resume._error() Uses Wrong ReceiptType
File: nodes/resume.py (line 323)
Problem: `_error()` helper emits receipts with `ReceiptType.TOOL_EXECUTION` for validation failures (NOT_FOUND, NOT_APPROVED, EXPIRED, TENANT_ISOLATION_VIOLATION, etc.). Validation failures are not tool executions.
Fix needed: Change to `ReceiptType.VALIDATION_FAILED` (if it exists in models) or a new purpose-appropriate type.

### FINDING-C5-005 (MEDIUM) — store_receipts_strict Event-Loop Thread Race
File: services/receipt_store.py (lines 500-506)
Problem: When `store_receipts_strict` is called FROM the event loop thread (not a worker thread), it schedules `flush_now()` as a task but does not await it. The function returns, and the pipeline continues before the receipt is durably written to Supabase. This is a non-deterministic window for YELLOW/RED receipt loss.
Fix needed: Callers of `store_receipts_strict` from async context should `await` an async version. Or: move YELLOW/RED receipt persistence to a dedicated coroutine.

### FINDING-C5-006 (MEDIUM) — council_service proposal/adjudication receipts missing required fields
File: services/council_service.py
Problem: council receipts (spawn, proposal, adjudication) have `receipt_hash: ""` and missing `redacted_inputs`, `redacted_outputs`, `idempotency_key`, `capability_token_id`. `receipt_type` is a free-form string, not from ReceiptType enum.
Impact: Minor — council is advisory infrastructure. No user-facing state changes.

### FINDING-C5-007 (LOW) — skillpack_factory registration receipt not persisted
File: services/skillpack_factory.py
Problem: `FactoryResult.receipt` is built but not stored via `store_receipts()`. Callers would need to call `store_receipts([result.receipt])` explicitly.
Impact: LOW — startup-time event only, no user-facing state change.

### FINDING-C5-008 (LOW) — kill_switch mode_change receipt uses "system" for suite_id/office_id
File: services/kill_switch.py (lines 218-219)
Problem: `_build_mode_change_receipt()` hardcodes `suite_id: "system"`, `office_id: "system"` which are not UUIDs. The receipt_store mapper converts non-UUID suite_id to UUID_NIL and will skip Supabase persistence (`suite_id == _UUID_NIL` guard).
Fix needed: Use a proper system UUID or reserve a well-known system suite_id.

## Confirmed OK (Not Violations)
- `supabase_update` calls are ALL on non-receipt tables (approval_requests, agent_semantic_memory, contracts, inbox_items, workflow_executions). Law #2 is not violated.
- Receipt store has no DELETE or UPDATE on the receipts table. Append-only confirmed.
- SCHEMA_VALIDATION_MODE defaults to "warn" — this is intentional for dev/test parity but means invalid receipts slip through in production. NEEDS VERIFICATION whether production sets STRICT mode.
- State machines (invoice, payment, contract, payroll) are pure logic — TransitionReceipts are in-memory. NEEDS VERIFICATION that callers persist them via receipt_store.

## Trace Chain Analysis
- Trace linkage: `correlation_id` flows correctly through the main pipeline (approval_check → execute → resume). `trace_id` is derived from `correlation_id` as fallback in `_map_receipt_to_row`. Good.
- Span chain: `span_id` and `parent_span_id` populated from `middleware/correlation` context. Not all receipt emitters set these explicitly — they rely on context propagation.
- Orphan risk: service-specific emitters (backup, deployment, RBAC) generate NEW `correlation_id = str(uuid.uuid4())` per receipt rather than inheriting from triggering request. This creates orphaned receipt chains for ops events.
