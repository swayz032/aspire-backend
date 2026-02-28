# Spec: High-Risk Approvals Require Ava Video (Gateway + Trust Spine Enforcement)

Version: 1.0  
Date: 2026-01-31  
Owner: Tonio (Product)  
Implementer: Claude Code (Engineering)  
Systems: trust-spine/, gateway/, brain/ (router is out of scope except logging)

---

## 0) Summary

This spec enforces the product rule:

> **High-risk approvals can only be approved and executed after a live Ava VIDEO session.**

UI may *display* high-risk items (Authority Queue on Home), but the backend must **fail-closed** if approval is attempted without valid video presence.

All execution remains governed by Trust Spine:
**A2A / Authority item → Approval → Outbox job → Executor → Provider call log → Receipt**.

---

## 1) Goals

### Functional goals
1. Prevent high-risk approvals from being executed outside of an Ava video session.
2. Provide a server-verifiable proof of “live Ava video presence”.
3. Bind approval to execution via capability enforcement (single-use, expiring).
4. Ensure all side effects execute only via `outbox_jobs` with idempotency and receipts.
5. Provide audit-ready receipts linking: authority item → approval → presence session → execution.

### Non-goals
- Implement payroll/transfers business logic for any specific provider.
- Implement the ritual/retention system.
- Redesign UI (Replit owns UI; Claude wires backend + data contracts).

---

## 2) Definitions

### Risk tier
- **low**: non-binding, reversible, low-impact actions.
- **medium**: customer-impacting but reversible / bounded actions.
- **high**: money movement, payroll changes, bank details, binding contracts, irreversible external actions.

### Required presence
- `none`
- `ava_voice`
- `ava_video`  ← mandatory for high-risk

Server must compute/validate required presence; do not trust client.

---

## 3) System boundary and “one execution path” invariant

### Invariant
All external side effects must flow through:

1. **Gateway** validates policy + presence + approvals and enqueues jobs
2. **Trust Spine** persists approval + receipts + outbox job
3. **Executor** (Edge or Node; pick one canonical) claims job and performs provider calls
4. **Provider call log + receipts** are written and linked to the trace

No UI, agent, or service may call providers directly.

---

## 4) Data Contracts (API payloads)

### 4.1 Authority Queue item (server → client)
```json
{
  "authorityItemId": "uuid",
  "suiteId": "uuid",
  "title": "Transfer $2,000 to Vendor",
  "summary": "ACH transfer to Vendor X",
  "riskTier": "high",
  "requiredPresence": "ava_video",
  "proposedAction": {
    "actionType": "money.transfer.create",
    "provider": "plaid_transfer",
    "inputs": { "amountCents": 200000, "currency": "USD" }
  }
}
```

### 4.2 Session start response (server → client)
```json
{
  "presenceSessionId": "uuid",
  "presenceToken": "plaintext-once",
  "expiresAt": "2026-01-31T12:00:00Z"
}
```

### 4.3 Approve request (client → server)
```json
{
  "suiteId": "uuid",
  "presenceSessionId": "uuid",
  "presenceToken": "plaintext",
  "confirmation": { "type": "typed", "value": "APPROVE" }
}
```

---

## 5) Trust Spine DB changes

> Add via canonical migrations under `trust-spine/03_SUPABASE_MIGRATIONS_ADDON/migrations/` (or equivalent).

### 5.1 `presence_sessions` (NEW)
Purpose: server-verifiable proof of “you are in Ava video session now”.

Fields:
- `presence_session_id uuid primary key`
- `suite_id uuid not null`
- `user_id uuid not null`
- `mode text check in ('ava_video','ava_voice') not null`
- `started_at timestamptz not null default now()`
- `expires_at timestamptz not null`
- `token_hash text not null` (store hash only)
- `status text check in ('active','ended','expired') not null default 'active'`
- `ended_at timestamptz`

Indexes:
- `(suite_id, user_id, status)`
- `(expires_at)`

RLS:
- A user can select only their own sessions for their suite.
- Inserts only for authenticated users in the suite.
- No access to plaintext tokens (never stored).

### 5.2 Authority queue persistence (IF NEEDED)
If Authority Queue is already derived from A2A items, ensure there is a stable identifier and status transitions.
Minimum requirements:
- stable `authority_item_id`
- `suite_id`
- `risk_tier`
- `required_presence`
- `proposed_action jsonb`
- `status` (pending/approved/denied/executed/expired)

If not present, add `authority_items` table.

---

## 6) Gateway API (enforcement plane)

Gateway is the **only** service allowed to:
- mint presence session tokens
- approve/deny authority items
- enqueue outbox jobs for execution

### 6.1 Start Ava presence session
**POST** `/sessions/ava/start`

Body:
```json
{
  "suiteId": "uuid",
  "mode": "ava_video",
  "context": { "authorityItemIds": ["uuid"] }
}
```

Response:
```json
{
  "presenceSessionId": "uuid",
  "presenceToken": "plaintext-once",
  "expiresAt": "iso8601"
}
```

Rules:
- Auth required.
- Validate user belongs to suite.
- Mode must be allowed (`ava_video` or `ava_voice`).
- Token is never logged; store only hash.
- Emit receipt: `presence.session.started`.

### 6.2 Approve authority item (requires presence for high risk)
**POST** `/authority-items/{id}/approve`

Body: see §4.3

Server checks (fail closed):
1. Load authority item + compute required presence based on risk tier.
2. If required presence is `ava_video`:
   - presence session exists and is `active`
   - not expired
   - mode is `ava_video`
   - token hash matches
3. Evaluate execution mode via Trust Spine `execution_controls` for `(suiteId, provider)`:
   - `DISABLED` → reject (`EXECUTION_DISABLED`)
   - `APPROVAL_ONLY` → record approval + receipt, do not enqueue outbox
   - `ENABLED` → proceed
4. Create approval record (`approval_requests` + `approval_events`).
5. If execution allowed:
   - enqueue outbox job with `requires_capability=true`
   - bind capability token to job (see §7)
6. Emit receipt: `authority.item.approved` with references:
   - `presenceSessionId`, `approvalId`, `outboxJobId` (nullable), execution mode

Response:
```json
{
  "approvalId": "uuid",
  "outboxJobId": "uuid-or-null",
  "receiptId": "uuid"
}
```

### 6.3 Deny authority item
**POST** `/authority-items/{id}/deny`

Presence requirement: recommended to require for `high` for audit symmetry, but may be relaxed.
Emit receipt: `authority.item.denied`.

---

## 7) Capability binding (approval → execution)

High-risk execution must be bound to approval via **capability**.
(You already have capability primitives in Trust Spine v5; this spec defines enforcement behavior.)

### 7.1 Issuance
When gateway enqueues an outbox job for a high-risk approval:
- Create a capability tied to:
  - `suite_id`, `authority_item_id`, `approval_id`, `outbox_job_id`
  - `actionType`, `provider`
  - `expires_at` (short TTL; suggest 10–30 minutes)
- Store hashed secret or capability record without exposing plaintext to client.

### 7.2 Consumption
Executor must refuse to execute a job with `requires_capability=true` unless it can **atomically consume** the capability for that job.

Failure code: `CAPABILITY_MISSING_OR_EXPIRED`.

---

## 8) Outbox job payload standard

Outbox job payload for approval execution:
```json
{
  "type": "authority.execute",
  "suiteId": "uuid",
  "authorityItemId": "uuid",
  "approvalId": "uuid",
  "provider": "plaid_transfer",
  "actionType": "money.transfer.create",
  "inputs": { "...": "..." },
  "trace": { "traceId": "uuid", "runId": "uuid", "spanId": "uuid" },
  "requiresCapability": true
}
```

Idempotency:
- Use a deterministic idempotency key derived from `(suiteId, authorityItemId, approvalId, actionType)`.

---

## 9) Receipts (audit trail)

Receipts must be emitted for:

### 9.1 Session start
Type: `presence.session.started`
- `presenceSessionId`, `mode`, `expiresAt`, optional `authorityItemIds`

### 9.2 Approval decision
Type: `authority.item.approved` / `authority.item.denied`
- `authorityItemId`
- `riskTier`, `requiredPresence`
- `presenceSessionId` (if required)
- `approvalId`
- `executionMode`
- `outboxJobId` (nullable)

### 9.3 Execution completion
Type: `authority.item.executed`
- `authorityItemId`, `outboxJobId`
- provider call refs
- outcome, timestamps

Receipt must include trace context: `trace_id`, `run_id`, `span_id`, `parent_span_id` where applicable.

---

## 10) Error taxonomy (UI + support-grade)

Return structured errors:

- `VIDEO_REQUIRED` → “Join Ava (Video) to approve this.”
- `PRESENCE_EXPIRED` → “Session expired. Rejoin Ava.”
- `EXECUTION_DISABLED` → “Execution disabled by admin.”
- `APPROVAL_ONLY_MODE` → “Approval recorded; execution paused.”
- `CAPABILITY_MISSING_OR_EXPIRED` → “Approval expired; please re-approve.”
- `NOT_AUTHORIZED`
- `NOT_FOUND`
- `VALIDATION_ERROR`

All errors should include a short `support_ref` with `trace_id` when available.

---

## 11) Security & privacy requirements

- Presence tokens are **never** stored in plaintext and **never logged**.
- Provider inputs/results must be redacted in logs; store redacted summaries only.
- RLS must enforce suite isolation for all new tables.
- Gateway must be the only component that can validate presence tokens.

---

## 12) Observability requirements

Every gateway request + executor run must log structured metadata:
- `suite_id`, `authority_item_id`, `approval_id`, `outbox_job_id`
- `trace_id`, `run_id`, `span_id`
- provider name, action type
- never log presence token or raw PII payloads

Metrics:
- approvals created (by risk tier)
- approvals rejected (by reason code)
- outbox queue depth and age
- execution success/failure rate by provider/action
- approval latency distribution

---

## 13) Acceptance tests (must pass)

### 13.1 Gateway tests
1. High-risk approve without presence → `VIDEO_REQUIRED`
2. High-risk approve with expired session → `PRESENCE_EXPIRED`
3. High-risk approve in `APPROVAL_ONLY` mode:
   - approval recorded
   - no outbox job
   - receipt exists
4. High-risk approve in `ENABLED` mode:
   - approval recorded
   - outbox job created
   - capability issued and bound

### 13.2 Executor tests
1. Capability required job:
   - executes if capability can be consumed
   - fails if capability missing/expired
2. Idempotency:
   - retries do not duplicate provider calls

### 13.3 UI behavior checks (manual)
- Home Authority Queue high-risk → CTA “Join Ava (Video)” only
- Approve/deny inside session only
- Receipt detail shows “Approved via Ava Video” and includes presenceSessionId metadata

---

## 14) Implementation order (fast path)

1. Add `presence_sessions` migration + RLS + indexes
2. Implement `POST /sessions/ava/start`
3. Implement `POST /authority-items/{id}/approve` and `{id}/deny`
4. Wire Trust Spine writes: approvals + receipts + outbox enqueue
5. Enforce capability consumption in executor
6. Add tests + smoke scripts

---

## 15) File/Module placement guidance

- `trust-spine/`:
  - migrations for `presence_sessions` (+ authority items if needed)
  - RPCs/helpers for session lookup + token hash verify (optional)
- `gateway/`:
  - endpoints + token mint/verify
  - policy checks + execution controls checks
  - outbox enqueue and receipt emission
- `executor/`:
  - capability enforcement before provider call
  - receipts on completion/failure

---

## 16) Notes for future provider approvals

This architecture supports reviewer expectations because:
- High-risk actions require explicit real-time human oversight (video)
- Approvals are bound to execution via capability and outbox
- Full auditable trace exists via receipts + replayability
- Kill switch exists via execution controls

