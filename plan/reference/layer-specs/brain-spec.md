# Brain Layer Specification

**Purpose:** Orchestration + routing + eval harness. The Single Brain (Law #1). Must NEVER execute side effects directly — only calls Gateway.
**Build Phase:** 1 (Orchestrator)
**Readiness:** Designed (10%) — Rich specs exist (personas, state machines, QA loop, router). ZERO LangGraph implementation.
**Ecosystem Path:** `platform/brain/`
**File Count:** 52 files

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      BRAIN LAYER                             │
│                                                              │
│  ┌──────────────┐     ┌──────────────┐    ┌──────────────┐  │
│  │ Intent Router │ ──→ │ State Machine│ ──→│  QA Gate     │  │
│  │ (which agent?)│     │ (which step?)│    │ (pass/fail?) │  │
│  └──────────────┘     └──────────────┘    └──────────────┘  │
│         │                    │                    │           │
│         ↓                    ↓                    ↓           │
│  ┌──────────────┐     ┌──────────────┐    ┌──────────────┐  │
│  │  LLM Router  │     │ Agent Persona│    │   Critics    │  │
│  │ (which model?)│     │ (system prompt│    │ (quality,    │  │
│  │              │     │  + constraints)│    │  policy,     │  │
│  │ FAST/PRIMARY/│     │              │    │  safety,     │  │
│  │ HIGH_RISK    │     │              │    │  tool_plan)  │  │
│  └──────────────┘     └──────────────┘    └──────────────┘  │
│                                                              │
│  Only outputs: proposals → Gateway                           │
│  Never executes provider calls directly                      │
└──────────────────────────────────────────────────────────────┘
```

**Laws Enforced:** #1 (Single Brain), #7 (Tools Are Hands)
**Key Constraint:** Brain ONLY calls Gateway. Brain NEVER touches Trust Spine or providers directly.

---

## Component Inventory

### 1. Agent Registry (10+ Personas)

**Zip Path:** Root `agent_kits/` (persona definitions) + `skillpacks/` (skill pack manifests)

Each agent has:
- `system_prompt.md` — LLM instructions defining personality and capabilities
- `constraints.yaml` — Hard limits (what the agent CANNOT do)
- `tools.yaml` — Tool allowlist (what the agent CAN use)
- `fewshots/` — Example interactions for in-context learning
- `style_guide.md` — Output formatting rules

**Shared Foundation (7 Governance Blocks):**
Every agent inherits these blocks (assembled by prompt compiler):

| Block | Purpose |
|-------|---------|
| `system_base` | Core identity: "You are an Aspire agent governed by 7 Laws" |
| `governance` | Aspire Laws reference, fail-closed behavior |
| `tool_rules` | Only use tools from your allowlist, validate capability tokens |
| `receipt_rules` | Every action produces a receipt, log reason codes |
| `escalation_rules` | When to escalate to human (risk tier thresholds) |
| `style_guide` | Output format, tone, structured responses |
| `output_schema` | JSON schema for agent outputs (tool calls, proposals, receipts) |

**Agent Roster:**

| Agent | Role | Domain | Default Model | Escalation Model | Key Tools |
|-------|------|--------|---------------|-----------------|-----------|
| **Ava** | Orchestrator | routing/governance | gpt-5.2-mini | gpt-5.2 | trustspine ops, directory lookup, policy eval, router select |
| **Sarah** | Front Desk | telephony/front-desk | gpt-5.2-mini | gpt-5.2 | telephony (lookup_call, collect_dtmf), trustspine ops |
| **Adam** | Research Desk | research/web-vendor | gpt-5.2-mini | gpt-5.2 | brave_search, tavily_search, places (6 providers), evidence capture |
| **Finn** | Money Desk | finance/money-movement | gpt-5.2-mini | gpt-5.2 | finance.money_events.read, propose_transfer (NEVER execute) |
| **Milo** | Payroll Desk | finance/payroll | gpt-5.2-mini | gpt-5.2 | gusto.read_payrolls, gusto.read_company_status. **payroll.submit = HARD DENY** |
| **Teressa** | Books Desk | finance/books | gpt-5.2-mini | gpt-5.2 | qbo.read_company, qbo.read_transactions, qbo.read_accounts |
| **Eli** | Inbox / Email | email/inbox | gpt-5.2-mini | gpt-5.2 | mail ops (list_threads, get_thread, create_draft, update_draft) |
| **Quinn** | Revenue Ops | billing/quotes-invoices | gpt-5.2-mini | gpt-5.2 | draft_quote, draft_invoice, trustspine ops |
| **Nora** | Conference Room | conference/meetings | gpt-5.2-mini | gpt-5.2 | conference.read_events, read_transcript, write_notes_internal. **DENIED: email, calendar, money, contracts** |
| **TEC** | Documents | documents | gpt-5.2-mini | gpt-5.2 | doc.render_pdf, doc.preflight, storage.put_object |
| **Clara** | Legal Desk | legal/contracts | gpt-5.2-mini | gpt-5.2 | contract.create, doc.render_pdf |

**Visibility Rules:** Finn, Milo, Teressa are `internal_frontend` only — they never face external users.
**Hard Denials:** Milo cannot `payroll.submit`. Nora cannot send emails, write calendar, create invoices, or move money.

### 2. State Machines (6 YAML Workflows)

**Zip Path:** `platform/brain/state_machines/`

Each state machine defines the step-by-step flow for a specific operation. States use typed actions (`llm_call`, `quality_gate`, `gateway_propose`).

| State Machine | States | Flow | Risk |
|--------------|--------|------|------|
| `invoice_draft.yaml` | start → extract → draft → critic → propose_send → done | Input → LLM extract → LLM draft → QA gate → Gateway proposal | MEDIUM |
| `inbox_triage.yaml` | Incoming email → classify → route → respond/escalate | Email → LLM classify → route to correct agent | LOW-MEDIUM |
| `legal_contract_send.yaml` | Draft → review → approve → send | Contract → LLM draft → QA (HIGH) → Authority → Send | HIGH |
| `conference_room.yaml` | Join → listen → participate → summarize → follow-up | Meeting → STT → NLP → Summary → Actions | LOW-MEDIUM |
| `mail_ops_triage.yaml` | Alert → classify → diagnose → remediate | Mail issue → triage → fix or escalate | MEDIUM |
| `n8n_ops_triage.yaml` | (empty — placeholder for "soon" stage) | — | — |

**Example: invoice_draft State Machine:**
```yaml
states:
  - id: start      → event: INPUT_RECEIVED → to: extract
  - id: extract    → action: llm_call (step_type: extract) → to: draft
  - id: draft      → action: llm_call (step_type: draft) → to: critic
  - id: critic     → action: quality_gate (risk: MEDIUM) → QA_PASSED → propose_send
  - id: blocked    → terminal (QA_BLOCKED)
  - id: propose_send → action: gateway_propose → to: done
  - id: done       → terminal
```

### 3. LLM Router

**Zip Path:** `platform/brain/router/router_policy.yaml`

Three model tiers with rule-based routing:

| Tier | Model Env | Use When |
|------|-----------|----------|
| `FAST_GENERAL` | `OPENAI_MODEL_FAST` | classify, extract, summarize + LOW risk |
| `PRIMARY_REASONER` | `OPENAI_MODEL_REASONER` | draft (LOW/MEDIUM), verify (LOW) |
| `HIGH_RISK_GUARD` | `OPENAI_MODEL_HIGH_RISK` | plan + verify (MEDIUM/HIGH) |

**Routing Rules:**
```yaml
rules:
  - when: { step_type: [classify, extract, summarize], risk_tier: [LOW] } → FAST_GENERAL
  - when: { step_type: [draft], risk_tier: [LOW, MEDIUM] } → PRIMARY_REASONER
  - when: { step_type: [verify], risk_tier: [LOW] } → PRIMARY_REASONER
  - when: { step_type: [plan, verify], risk_tier: [MEDIUM, HIGH] } → HIGH_RISK_GUARD
```

**Fallback Chain:** HIGH_RISK_GUARD → PRIMARY_REASONER → FAST_GENERAL

**Per-Agent Routers:**
- `rules/adam_researchdesk_router.yaml`
- `rules/finn_moneydesk_router.yaml`
- `rules/milo_payrolldesk_router.yaml`
- `rules/teressa_booksdesk_router.yaml`
- `rules/conference_router.ts`
- `model_policies/conference.yaml`
- `tool_policies/conference.yaml`
- `council_router.yaml`
- `internal_learning_router.yaml`

### 4. QA Loop (Enterprise Quality Gate)

**Zip Path:** `platform/brain/qa/`
**Flow:** Primary Agent → Critic(s) → Fix → Gate (pass/block)

**Critic Policy (`critic_policy.yaml`):**

| Risk Tier | Required Critics | Max Revision Loops |
|-----------|-----------------|-------------------|
| **LOW** | quality | 1 |
| **MEDIUM** | quality + policy | 2 |
| **HIGH** | quality + policy + safety + tool_plan | 2 |

**Surfaces (Context-Aware Escalation):**

| Surface | Default Risk | Escalates To | Escalation Triggers |
|---------|-------------|--------------|---------------------|
| `frontend_ava` | LOW | MEDIUM | downloadable artifact, external send, legal/financial language |
| `admin_agents` | MEDIUM | HIGH | provider write, permission change, money movement |
| `conference_nora` | LOW | MEDIUM | money/contract decisions, external recap send |

**Critics (5 total):**

| Critic | Prompt | Rubric | Purpose |
|--------|--------|--------|---------|
| `quality` | `prompts/quality_critic.prompt.md` | `rubrics/quality_rubric_v1.yaml` | Completeness, accuracy, formatting |
| `policy` | `prompts/policy_critic.prompt.md` | `rubrics/policy_rubric_v1.yaml` | Aspire Laws compliance |
| `safety` | `prompts/safety_critic.prompt.md` | `rubrics/safety_rubric_v1.yaml` | Prompt injection, PII leakage, harmful content |
| `tool_plan` | `prompts/tool_plan_critic.prompt.md` | `rubrics/tool_plan_rubric_v1.yaml` | Tool call correctness, scope, idempotency |
| `evidence` | `prompts/evidence_critic.prompt.md` | `rubrics/evidence_rubric_v1.yaml` | Source verification, citation accuracy |

**Integration:** `platform/brain/workflows/quality_gate.workflow.ts`

### 5. Eval Harness

**Zip Path:** `platform/brain/eval/`

| Eval Suite | Fixtures | Purpose |
|-----------|----------|---------|
| `conference/` | 3 test cases (email followup, talk turns, high risk interrupt) | Conference agent correctness |
| `council/` | Example incident fixture | Multi-agent deliberation quality |
| `learning/` | Threshold definitions (`thresholds.yaml`) | Learning loop accuracy |
| `cases/` | Clara contracts, general samples | Cross-agent eval |

**Runner:** `run_eval.ts` — executes fixtures against agents, scores against thresholds

### 6. Prompt Compiler

**Zip Path:** `platform/brain/` (implied `assemble.ts` pattern)
**Purpose:** Dynamically constructs agent prompts by assembling:
1. Shared foundation blocks (7 governance blocks)
2. Agent-specific system prompt
3. Context injection (suite_id, current state, conversation history)
4. Tool definitions from allowlist

### 7. Workflows

**Zip Path:** `platform/brain/workflows/`

| Workflow | Purpose |
|----------|---------|
| `quality_gate.workflow.ts` | QA loop orchestration |
| `council_deliberation.workflow.ts` | Multi-agent council (Meeting of Minds) |
| `evidence_pack_builder.ts` | Research evidence compilation |
| `learning_bundle_builder.workflow.ts` | Learning loop data packaging |
| `legal_contract_bundle.workflow.ts` | Legal document assembly |

### 8. Validators

**Zip Path:** `platform/brain/validators/`

| Validator | Purpose |
|-----------|---------|
| `council_output_validator.ts` | Validates council deliberation outputs |
| `critic_output_validator.ts` | Validates critic responses against rubric schema |

---

## V1 Minimum Viable Orchestrator (Co-Founder Recommendation)

For Phase 1, implement the **simplest possible brain** that enforces all laws:

1. **Single agent:** Ava (voice-first interface)
2. **Single state machine:** `intent → extract → draft → qa_gate → propose → done`
3. **Single model tier:** PRIMARY_REASONER only (add FAST/HIGH_RISK in Phase 2)
4. **QA Loop:** Quality + Policy critics (skip safety + tool_plan for GREEN/YELLOW)
5. **No multi-agent:** A2A messaging deployed but not wired
6. **No council:** Meeting of Minds is Capital Quarter

**This gives us:** Governed single-agent execution with receipts, approval flows, and basic quality assurance. Law-compliant from day one.

---

## Implementation Readiness: 10%

| Component | Status | What's Needed |
|-----------|--------|---------------|
| Agent Personas | **Designed** | Load into prompt compiler |
| State Machines | **Designed** | Implement LangGraph graph from YAML |
| LLM Router | **Designed** | Implement tier selection + fallback |
| QA Loop | **Designed** | Implement critic pipeline + revision loop |
| Eval Harness | **Designed** | Implement test runner |
| Prompt Compiler | **Designed** | Build assembly logic |
| LangGraph Integration | **Not Started** | BIGGEST RISK — core orchestrator from scratch |

---

**End of Brain Layer Specification**
