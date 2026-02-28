# Patent Blueprint → Implementation Map (Single Source of Truth)

This document turns the patent pack into a **checklist**:
**Invariant → where enforced → how proven (tests/evidence).**

> Rule: Patent pack is reference. Canonical implementation contracts live in `/contracts/`.

---

## Blueprint invariants (must hold)

### 1) Approval-only execution for high-risk actions
**Invariant:** High-risk actions cannot execute without approval.  
**Implementation:** 
- `trust-spine/` approvals tables + approval events
- `gateway/` must require approval for `risk_tier=high`
- `executor/` runs only `outbox_jobs` created by gateway
**Proof:**
- Gateway test: high-risk approve without required conditions → 403
- Integration test: no outbox job can be created for high-risk without approvalId

---

### 2) High-risk approvals require Ava VIDEO presence
**Invariant:** High-risk approvals must occur inside a live Ava video session (fail-closed server-side).  
**Implementation:** 
- Spec: `docs/specs/SPEC_HighRisk_Approvals_AvaVideo_Gateway_TrustSpine.md`
- DB: `presence_sessions` table (hashed tokens, TTL)
- Gateway: `/sessions/ava/start`, `/authority-items/{id}/approve`
**Proof:**
- Contract tests:
  - approve high-risk without presence → `VIDEO_REQUIRED`
  - expired presence → `PRESENCE_EXPIRED`
  - APPROVAL_ONLY mode records approval but does not enqueue execution

---

### 3) Capability-scoped execution (approval → execution binding)
**Invariant:** Even if approval exists, execution requires a single-use capability bound to the job.  
**Implementation:**
- `trust-spine/` capability token primitives + consume RPC
- `executor/` must consume capability before any provider call
**Proof:**
- Executor test: capability missing/expired → job fails without provider call

---

### 4) No shadow execution paths
**Invariant:** No UI, agent, or service can call providers directly.  
**Implementation:**
- Only gateway can enqueue outbox jobs
- Only executor performs provider calls
**Proof:**
- Static check: disallow provider SDK imports outside adapters/executor
- Runtime: audit log for any non-executor provider call attempts (optional)
- CI: lint rule or grep-based gate

---

### 5) Immutable receipts and auditable traceability
**Invariant:** Every risky action produces a tamper-evident receipt linked to approvals and execution.  
**Implementation:**
- `trust-spine/` receipts table (hash, optional signature)
- Gateway emits receipts for session start + approval decision
- Executor emits receipts for completion/failure
**Proof:**
- Receipt replay: given a `trace_id`, rebuild approval + execution chain
- DB constraint tests: receipts are append-only (no updates/deletes)

---

### 6) Tenant isolation
**Invariant:** No cross-company data leakage.  
**Implementation:**
- RLS policies on all tenant-bound tables using `suite_id`
- Helper functions: `auth.current_suite_id()` etc.
**Proof:**
- RLS “evil tests” in CI: user from suite A cannot read suite B

---

### 7) A2A as the canonical task substrate (internal agent inbox)
**Invariant:** Work is expressed as tasks/messages, not ad-hoc function calls.  
**Implementation:**
- A2A inbox/outbox tables or authority items derived from A2A
- Router/orchestrator (Ava) emits/consumes A2A items
**Proof:**
- Trace shows: A2A item created → approval event → outbox job → receipt

---

## What to do with this document
- Treat it as the master checklist for Claude.
- Every invariant must have:
  1) enforcement location(s)
  2) at least one automated test
  3) at least one “evidence pack” artifact (receipt bundle or replay output)
