# Cross-Layer Integration Map

**Purpose:** How Aspire's layers connect. Data flow, error propagation, receipt threading, and capability token lifecycle.

---

## The Execution Pipeline (Full Layer Trace)

```
USER INTENT
     │
     ↓
┌─────────────┐
│   INGEST    │  Standardized intent: { suite_id, intent_type, risk_tier, payload }
│   (API)     │  → Creates inbox_item in Trust Spine
└──────┬──────┘
       │
       ↓
┌─────────────┐
│   BRAIN     │  1. Router selects agent (Ava, Finn, Adam, etc.)
│             │  2. State machine drives flow (extract → draft → QA → propose)
│             │  3. LLM Router picks model tier (FAST / PRIMARY / HIGH_RISK)
│  Law #1:    │  4. QA Loop validates output (critics based on risk tier)
│  Single     │  5. Outputs a PROPOSAL (never executes directly)
│  Brain      │
└──────┬──────┘
       │ proposal
       ↓
┌─────────────┐
│   GATEWAY   │  1. Evaluates policy (tools_catalog + agent capabilities)
│             │  2. Determines risk tier + approval requirement
│  Law #4:    │  3. Safety guards check (video_presence, high_risk_interrupt)
│  Risk Tiers │  4. PII redaction on inputs/outputs
│  Law #5:    │
│  Cap Tokens │     GREEN          YELLOW/RED         DENIED
│             │       ↓               ↓                 ↓
└─────┬───────┘   Auto-approve   Authority Queue    Receipt (denied)
      │               │               │
      │               ↓               ↓
      │         Enqueue Outbox    User Approves
      │               │               │
      ↓               ↓               ↓
┌─────────────┐
│ TRUST SPINE │  1. Outbox worker claims job (FOR UPDATE SKIP LOCKED)
│             │  2. Capability token minted (<60s, scoped)
│  Law #2:    │  3. Outbox executor calls provider via Gateway tools
│  Receipts   │  4. Receipt generated (hash-chained, immutable)
│  Law #3:    │  5. Receipt includes: outcome, correlation_id, approval_evidence
│  Fail Closed│
│  Law #6:    │  Every step scoped to suite_id (RLS enforced)
│  Isolation  │
└──────┬──────┘
       │
       ↓
┌─────────────┐
│  SUPABASE   │  Postgres (state persistence) + Edge Functions (serverless compute)
│  (State)    │  All data RLS-protected by suite_id
└─────────────┘
```

---

## Error Propagation

Errors flow UPWARD through the stack. Every error generates a receipt.

```
Provider Error (e.g., Stripe API 500)
     │
     ↓
Trust Spine: Receipt { outcome: "failed", reason_code: "provider_error", ... }
     │
     ↓
Gateway: Decision { retry_allowed: false } (Brain decides retry, not Gateway)
     │
     ↓
Brain: Orchestrator evaluates:
  - Is retry safe? (idempotency key present?)
  - Has max retry been reached?
  - Should user be notified?
     │
     ↓
Brain outputs: NEW proposal (retry) OR receipt (final failure + user notification)
```

**Key Rule:** Gateway and Trust Spine NEVER retry autonomously. Only the Brain decides whether to retry (Law #1).

---

## Receipt Threading (Correlation)

Every operation has a `correlation_id` that threads through ALL layers:

```
correlation_id: "abc-123"
  │
  ├── inbox_item.id (Ingest)
  ├── state_machine_run.id (Brain)
  ├── policy_decision.id (Gateway)
  ├── approval_request.id (Trust Spine, if YELLOW/RED)
  ├── outbox_job.id (Trust Spine)
  ├── capability_token.id (Trust Spine)
  ├── provider_call_log.id (Trust Spine)
  └── receipt.id (Trust Spine — final audit record)
```

**Reconstruction:** Given a `correlation_id`, you can reconstruct the ENTIRE execution path from intent to receipt. This is the basis of the Replay Harness (Gate 04 / Phase 4).

---

## Capability Token Lifecycle

```
Brain decides: "Execute invoice creation via Stripe"
     │
     ↓
Brain → Gateway: Proposal { action: "stripe.invoice.create", suite_id, ... }
     │
     ↓
Gateway: Policy check → YELLOW → User approval required
     │
     ↓
Trust Spine: approval_request created → Authority Queue
     │
     ↓
User approves (via mobile app / video presence)
     │
     ↓
Trust Spine: Mints capability_token {
  token_id: "uuid",
  suite_id: "uuid",
  tool: "stripe.invoice.create",
  scopes: ["invoice.write"],
  expires_at: "<now + 60s>",
  signature: "HMAC-SHA256"
}
     │
     ↓
Trust Spine → Outbox: Job enqueued with capability_token
     │
     ↓
Outbox Worker: Claims job, validates token (server-side):
  - Is token expired? (<60s check)
  - Is token scoped to this suite_id?
  - Is token scoped to this tool?
  - Is signature valid?
     │
     ↓
Outbox Executor: Calls Gateway tool → Stripe API
     │
     ↓
Trust Spine: Receipt { outcome: "success", capability_token_id: "...", ... }
     │
     ↓
Token consumed (single-use, now invalid)
```

---

## Layer Boundary Rules

| From | To | Allowed Communication |
|------|----|-----------------------|
| Ingest → Brain | Intent forwarding | Inbox item → orchestrator picks up |
| Brain → Gateway | Proposals only | Brain proposes, Gateway evaluates |
| Gateway → Trust Spine | Policy decisions + outbox jobs | Gateway enqueues, Trust Spine executes |
| Trust Spine → Providers | Via outbox executor | Capability token required, receipt generated |
| Brain → Trust Spine | **FORBIDDEN** | Brain must go through Gateway |
| Brain → Providers | **FORBIDDEN** | Brain never calls providers directly |
| Gateway → Providers | **FORBIDDEN** (direct) | Gateway tools call via outbox executor |
| n8n → Brain | Trigger only | n8n triggers orchestrator, never decides |
| n8n → Providers | **FORBIDDEN** | n8n cannot call providers |
| Control Plane → All | Config updates via Trust Spine | Proposal → approval → receipt for all config changes |

---

## A2A Messaging (Agent-to-Agent)

```
Agent A (Brain): "I need research on this vendor"
     │
     ↓
Trust Spine: A2A task created {
  from_agent: "ava",
  to_agent: "adam",
  task: "research_vendor",
  payload: { vendor_name: "..." },
  suite_id: "...",
  status: "pending"
}
     │
     ↓
Brain: Adam agent picks up A2A task
     │
     ↓
Adam: Executes research → proposal → Gateway → receipt
     │
     ↓
Trust Spine: A2A task status → "completed"
     │
     ↓
Brain: Ava receives result, continues original workflow
```

**State Machine:** `pending → claimed → in_progress → completed | failed | expired`
**Every transition generates a receipt** (Law #2).

---

## Control Plane Config Flow

When a config change is needed (e.g., update agent prompt, change risk tier):

```
Control Plane: Registry item created (draft)
     ↓
Control Plane: State transition → staged
     ↓
Control Plane → Trust Spine: Proposal for activation
     ↓
Trust Spine: Approval request → Authority Queue
     ↓
Admin approves
     ↓
Control Plane: State transition → active
     ↓
Control Plane Executor: Applies config change (idempotent)
     ↓
Trust Spine: Receipt { action: "config_change", ... }
     ↓
Rollout: 0% → 1% → 10% → 50% → 100% (canary progression)
```

---

## Data Flow Summary by Operation Type

### GREEN Operation (e.g., "What's on my calendar?")

```
Ingest → Brain (Ava → extract) → Gateway (policy: GREEN, auto-approve)
→ Trust Spine (mint token, outbox, execute) → Google Calendar API
→ Receipt (success) → Brain (summarize) → User
```
**No human approval needed.** Total latency target: <5s.

### YELLOW Operation (e.g., "Send this invoice")

```
Ingest → Brain (Quinn → draft → QA gate) → Gateway (policy: YELLOW)
→ Trust Spine (approval request → Authority Queue)
→ User approves (mobile app)
→ Trust Spine (mint token, outbox, execute) → Stripe API
→ Receipt (success) → Brain (confirm) → User
```
**Requires user confirmation.** Total latency target: <30s (excluding user decision time).

### RED Operation (e.g., "Pay this contractor $5,000")

```
Ingest → Brain (Finn → draft → QA gate [quality + policy + safety + tool_plan])
→ Gateway (policy: RED, video_presence guard triggered)
→ Trust Spine (approval request → Authority Queue)
→ User approves ON CAMERA (Hot state, video presence verified)
→ Trust Spine (mint token, outbox, execute) → Moov/Plaid API
→ Receipt (success, approval_evidence includes video session_id)
→ Brain (confirm) → User
```
**Requires explicit authority with video presence.** Total latency target: <5s (excluding user decision time).

---

**End of Cross-Layer Integration Map**
