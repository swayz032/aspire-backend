# Trust Spine Layer Specification

**Purpose:** Governance substrate — enforces Aspire's 7 Immutable Laws at the database and edge function level.
**Build Phase:** 0B (Tower Setup)
**Readiness:** Scaffolded (70%) — Migrations, edge functions, and Go verifier exist. Need deployment + executor implementation.
**Ecosystem Path:** `platform/trust-spine/`
**File Count:** 872 files (largest layer)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    TRUST SPINE LAYER                         │
│                                                             │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ Inbox   │  │ Policy   │  │ Approval │  │  Outbox    │  │
│  │ (Ingest)│→ │ Engine   │→ │ Workflow │→ │ (Executor) │  │
│  └─────────┘  └──────────┘  └──────────┘  └────────────┘  │
│       │            │              │              │          │
│       └────────────┴──────────────┴──────────────┘          │
│                         ↓                                   │
│              ┌──────────────────┐                           │
│              │   RECEIPTS       │                           │
│              │ (append-only,    │                           │
│              │  hash-chained)   │                           │
│              └──────────────────┘                           │
│                         ↓                                   │
│              ┌──────────────────┐                           │
│              │   A2A MESSAGING  │                           │
│              │ (agent-to-agent  │                           │
│              │  task contracts) │                           │
│              └──────────────────┘                           │
└─────────────────────────────────────────────────────────────┘
```

**Laws Enforced:** #2 (Receipts), #3 (Fail Closed), #5 (Capability Tokens), #6 (Tenant Isolation)

---

## Authoritative Rules (from `00_START_HERE/README.md`)

1. **DB truth = Claude Handoff 4.0 (Phase 0 bootstrap)** — Do not rename or re-type existing bootstrap tables/functions
2. **API truth = `02_CANONICAL/openapi.unified.yaml`** — Unified contract for edge functions + clients
3. **Identity = Option B** — `suite_id` is canonical (UUID). `tenant_id` is legacy/compat, auto-derived via `app.suites`
4. **Additive only** — Everything new ships as add-on migrations + new endpoints

---

## Component Inventory

### 1. Database Migrations

**Deployment Order:**
1. Claude Handoff Phase 0 bootstrap migrations (unchanged) — `01_ORIGINAL_INPUTS/claude_handoff_4_0/`
2. Trust Spine addon migrations — `03_SUPABASE_MIGRATIONS_ADDON/migrations/` (~42 files)
3. A2A Inbox addon — `06_ADDONS/A2A_INBOX_V6/02_DB/migrations/` (7 files)

**Migration Count Reconciliation:**
- Plan documents now reference CANONICAL_PATHS.md for exact migration paths and counts
- The "86 base" refers to Claude Handoff 4.0 bootstrap migrations (separate zip package)
- Trust Spine addon contains ~42 migration files (includes suite/office identity, inbox/outbox, approval_events, receipts)
- A2A addon contains 7 migrations
- **ACTION REQUIRED:** Physically verify count of Claude Handoff 4.0 base migrations before Phase 0B deployment

**Key Tables Created:**
- `suites`, `offices`, `tenant_memberships` — Identity + tenancy
- `inbox_items` — Intent ingest (suite_id scoped)
- `outbox_jobs` — Durable execution queue
- `approval_requests`, `approval_events` — Governance gates
- `receipts` — Append-only, hash-chained audit trail
- `provider_call_log` — External API call tracking (redacted payloads)
- `capability_tokens` — Short-lived execution authorization
- `policy_rules`, `policy_decisions` — Policy engine
- `presence_sessions` — Video presence gating (Hot state)

**RLS Enforcement:** All tables have Row-Level Security policies using `suite_id` context:
```sql
-- Context-setting pattern (applied per request)
SET LOCAL app.current_suite_id = '<uuid>';
-- RLS policy pattern
CREATE POLICY tenant_isolation ON <table>
  USING (suite_id = current_setting('app.current_suite_id')::uuid);
```

### 2. Edge Functions (8 total)

**Main Functions (5):**

| Function | Purpose | Input | Output | Zip Path |
|----------|---------|-------|--------|----------|
| `inbox` | Intent ingest — accepts proposals, creates inbox items | `{ suite_id, intent_type, payload }` | `{ inbox_item_id }` | `04_EDGE_FUNCTIONS/supabase/functions/inbox/` |
| `policy-eval` | Policy evaluation — evaluates rules, returns allow/deny/require-approval | `{ suite_id, action, context }` | `{ decision_id, approval_required, risk_tier }` | `04_EDGE_FUNCTIONS/supabase/functions/policy-eval/` |
| `outbox-worker` | Claims pending outbox jobs using atomic RPC | `{ suite_id, limit, worker_id }` | `{ jobs[] }` | `04_EDGE_FUNCTIONS/supabase/functions/outbox-worker/` |
| `outbox-executor` | Executes claimed jobs via provider adapters | `{ job_id, capability_token }` | `{ receipt_id, outcome }` | `04_EDGE_FUNCTIONS/supabase/functions/outbox-executor/` |
| `approval-events` | Processes approval decisions, triggers downstream | `{ approval_id, decision, actor }` | `{ event_id }` | `04_EDGE_FUNCTIONS/supabase/functions/approval-events/` |

**A2A Functions (3):**

| Function | Purpose | Zip Path |
|----------|---------|----------|
| `a2a-inbox-enqueue` | Enqueue agent-to-agent task | `06_ADDONS/A2A_INBOX_V6/` |
| `a2a-inbox-claim` | Claim A2A task for processing | `06_ADDONS/A2A_INBOX_V6/` |
| `a2a-inbox-transition` | Transition A2A task state | `06_ADDONS/A2A_INBOX_V6/` |

**Security:** Worker endpoints guarded by `X-Worker-Secret` header. Public API uses Bearer JWT auth.

**Required Env Vars:** `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` (worker), `TRUST_SPINE_WORKER_SECRET` (worker)

### 3. Go Receipt Verification Service

**Zip Path:** `09_RECEIPTS/` (verification algorithm)
**Purpose:** Independent hash-chain verification microservice
**Algorithm:**
- SHA-256 hash per receipt (content + previous hash = chain)
- EdDSA signatures for cryptographic non-repudiation
- Genesis block validation (first receipt in chain)
- Full chain walk for integrity verification

**API:** HTTP service exposing verify endpoints. Runs independently of Supabase.

### 4. Outbox Pattern (Durable Execution)

**Claim RPC:**
```sql
public.claim_outbox_jobs(p_suite_id uuid, p_limit int, p_worker_id text)
```
- Uses `FOR UPDATE SKIP LOCKED` for atomic claim
- At-least-once delivery guarantee
- Idempotency via `idempotency_key` on outbox jobs
- Dead-letter after max retry attempts (configurable)
- Exponential backoff between retries

### 5. A2A Messaging (Agent-to-Agent)

**Task Contract:** Agent A creates a task → assigned to Agent B → Agent B processes → emits receipt
**State Machine:** `pending` → `claimed` → `in_progress` → `completed` | `failed` | `expired`
**Receipts Bridge:** Every A2A state transition generates a receipt (Law #2 compliance)
**Zip Path:** `06_ADDONS/A2A_INBOX_V6/`

### 6. Policy Engine

**Schema:** `policy_rules` table with rule matching
**RPC:** `trust_policy_eval(suite_id, action, context)` → returns `{ decision, risk_tier, approval_required }`
**Default Policies:** Pre-loaded for all standard risk tiers (green = auto, yellow = approval, red = authority)
**Zip Path:** `10_POLICY_ENGINE/`

### 7. Trust Spine Addons

| Addon | Purpose | Zip Path |
|-------|---------|----------|
| **A2A Inbox V6** | Agent-to-agent messaging | `06_ADDONS/A2A_INBOX_V6/` |
| **Replay Harness V1** | Event sourcing — reconstruct state from receipts | `06_ADDONS/REPLAY_HARNESS_V1/` |
| **Executor Node TS V1** | Node.js executor scaffold | `06_ADDONS/EXECUTOR_NODE_TS_V1/` |
| **Observability OTEL V1** | OpenTelemetry instrumentation | `06_ADDONS/OBSERVABILITY_OTEL_V1/` |
| **Behavior Flywheel V1** | Learning loop — what to improve next | `06_ADDONS/BEHAVIOR_FLYWHEEL_V1/` |
| **Legal Desk Clara V1** | Legal-specific policies, receipts, templates | `06_ADDONS/LEGAL_DESK_CLARA_V1/` |

---

## Phase 0B Deployment Checklist

1. [ ] Verify Claude Handoff 4.0 base migration count
2. [ ] Apply base migrations to Supabase instance
3. [ ] Apply Trust Spine addon migrations (42 files)
4. [ ] Apply A2A addon migrations (7 files)
5. [ ] Deploy 5 main edge functions
6. [ ] Deploy 3 A2A edge functions
7. [ ] Set environment variables (SUPABASE_URL, keys, worker secret)
8. [ ] Verify RLS policies on all tables
9. [ ] Run Go receipt verifier smoke test
10. [ ] Test outbox claim RPC with sample data
11. [ ] Verify hash-chain integrity on test receipts

---

## Implementation Readiness: 70%

| Component | Status | What's Needed |
|-----------|--------|---------------|
| Migrations | **Ready** | Verify count, apply in order |
| Edge Functions | **Scaffolded** | Complete TS stubs, test against Supabase |
| Go Verifier | **Scaffolded** | Build binary, deploy, test |
| Outbox Worker | **Scaffolded** | Implement claim → execute → receipt flow |
| Policy Engine | **Designed** | Implement rule evaluator |
| A2A Messaging | **Scaffolded** | Implement worker, test state transitions |
| Replay Harness | **Designed** | Full implementation needed (Phase 4) |

---

**End of Trust Spine Layer Specification**
