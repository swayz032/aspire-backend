---
phase: "1"
name: "Capability Tokens + Receipt Ledger + A2A Router (PARALLEL: 1A Substrate Validation + 1B Intelligence Integration)"
status: "not_started"
blocking_phase: "0B"
blocks_phases: ["2", "3", "4"]
duration_estimate: "5-6 weeks (accelerated via Trust Spine pre-built substrate)"
gates_targeted: [6, 7]
priority: "critical"
hardware_required: "Skytech Shadow (Ryzen 7 7700, RTX 5060, 32GB DDR5)"
cost: "$10-20/mo (OpenAI API usage during development)"
handoff_provides: "Gateway scaffold + Trust Spine substrate (deployed in Phase 0B) + A2A Router scaffolds"
layer_specs: ["reference/layer-specs/brain-spec.md", "reference/layer-specs/gateway-spec.md"]
implementation_readiness: "Brain 10%, Gateway 15% — richest specs but zero LangGraph code. BIGGEST TIMELINE RISK."
---

# PHASE 1: Core Orchestrator + Safety Systems

## 🔗 API WIRING TASKS (v4.2)

**This phase wires core infrastructure APIs to the orchestrator:**

| API Service | Wiring Task | Risk Tier | Verification |
|-------------|-------------|-----------|--------------|
| **OpenAI** | LangGraph brain integration | - | LLM calls succeed |
| **Supabase** | RLS policies, Auth integration | - | Tenant isolation verified |
| **Upstash** | Outbox queue integration | - | Jobs process correctly |
| **AWS S3** | Receipt blob storage | - | Artifacts stored |

**Orchestrator API Endpoints to Create:**
- `POST /v1/intents` → Intent ingestion
- `POST /v1/receipts` → Receipt creation
- `POST /v1/receipts/verify-run` → Hash chain verification
- `POST /v1/policy/evaluate` → ALLOW/DENY/REQUIRE_APPROVAL
- `POST /v1/capability-tokens/mint` → Token minting

**Schema Files to Implement:**
- `plan/schemas/capability-token.schema.v1.yaml` → Token definition
- `plan/registries/gate-satisfaction.yaml` → Gate tracking

**Gates to Satisfy:**
- Gate 6: Receipts Immutable (append-only, hash-chained)
- Gate 7: RLS Isolation (zero cross-tenant leakage)

---

## Objective

Build the LangGraph "Brain" with Ava integration, receipt generation, and **CRITICAL safety systems** (prompt injection defense, guardrails, DLP/PII redaction).

**⚠️ CRITICAL NOTE**: This phase includes 3 safety systems promoted from "Lean: Soon" to "Lean: Yes" based on ultra-deep infrastructure analysis. Without these, Aspire is vulnerable to prompt injection attacks and PII leaks in high-risk operations (Quinn Invoicing, Eli Inbox, Finn Money Desk).

**Trust/Liability Confidence**: Increases from 50% → 90% with safety systems implemented.

---

## Parallel Execution Strategy (NEW)

**CRITICAL CHANGE:** Phase 1 bifurcates into two PARALLEL tracks:

- **Phase 1A (Week 3-5, 3 weeks):** Substrate Validation - Test Trust Spine infrastructure
- **Phase 1B (Week 4-7, 4 weeks):** Intelligence Integration - Build LangGraph + Safety Gateway

**Timeline Savings:** Parallel execution saves 1 week (7 weeks total vs. 8 sequential)

**Merge Point:** Week 6-7 - Integration testing (substrate + intelligence together)

---

## Trust Spine Ecosystem Documentation (IMPORTANT)

**📚 Comprehensive testing and validation documentation exists in the Trust Spine package:**

### Phase 1A: Substrate Validation Resources
- **Navigation Guide:** See `Trust-Spine-Package-Navigation.md` (in this plan directory) for complete testing workflow
- **E2E Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/` for concrete test suites:
  - `sql/tenant_isolation.sql` - Zero cross-tenant leakage test (CRITICAL)
  - `sql/outbox_concurrency.sql` - FOR UPDATE SKIP LOCKED concurrency test
  - `sql/idempotency_replay.sql` - Duplicate request handling test
  - `sql/receipt_hash_verify.sql` - Hash chain integrity verification
- **Test Execution Guide:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/README.md` for test order and expected results
- **Validation Runbook:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_2_FEATURE_TEMPLATE/RUNBOOKS/` for validation procedures

### Phase 1B: Intelligence Integration Resources
- **API Contracts:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/02_CANONICAL/openapi.unified.yaml` for:
  - Capability token API specification (token schema, minting, verification)
  - Receipts API specification (creation, retrieval, hash chain)
  - Policy evaluation API (ALLOW/DENY/REQUIRE_APPROVAL)
- **Integration Runbook:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_3_SKILL_PACKS_CERTIFICATION/RUNBOOKS/` for LangGraph wiring guide
- **Definition of Done:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/PHASE_3_SKILL_PACKS_CERTIFICATION/DEFINITION_OF_DONE/` for integration gates

### Observability & Monitoring
- **SLI/SLO Definitions:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/ops/SLI_SLO.md` for metrics to monitor during Phase 1
- **Incident Runbooks:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/ops/runbooks/` for troubleshooting (APPROVALS_BACKLOG.md, OUTBOX_STUCK.md)

**⭐ Start Here:** Read `Trust-Spine-Package-Navigation.md` → then `13_E2E_TESTS/README.md` for Phase 1A testing workflow.

---

## Dependencies

**Requires (Blocking):**
- Phase 0B: Trust Spine Deploy (COMPLETE 2026-02-10 — 49 migrations, 5 Edge Functions, RLS verified, Desktop integrated)
- Schemas designed in Phase 0A (COMPLETE — deployed to Supabase)

**Blocks (Downstream):**
- Phase 2: Skill Pack Integration (needs orchestrator + receipts working)
- Phase 3: Mobile App (needs orchestrator API)
- Phase 4: Production Hardening (needs base system to harden)

---


## Handoff Package Integration - Bifurcated Implementation

**Status:** 🔄 60-70% COMPLETE VIA HANDOFF

The handoff package provides production-ready infrastructure that splits Phase 1 into two parallel tracks.

---

### Track 1A: Gateway & Infrastructure (FROM HANDOFF - Ready to Deploy)

**Status:** ✅ PRODUCTION-READY CODE AVAILABLE

**What's Included:**
- **Go HTTP Gateway Scaffold** (phase1_platform_v2/gateway_scaffold/)
  - Policy enforcement engine
  - Approval binding + TOCTOU prevention
  - Capability token validation
  - DLP/PII redaction rules
  - Receipt generation + storage
  - Health checks + SLO monitors

- **Policy Matrix** (policy/policy/policy_matrix.yaml)
  - YAML-based policy specification
  - Tool → category → risk tier mapping
  - Approval requirements
  - Parameter allowlists + constraints
  - Validator script included

- **Test Harnesses** (phase1_platform_v2/tests/harness/)
  - TC-02: Receipt integrity (hash chain validation)
  - TC-03: PII redaction (fixture-driven)
  - Prompt injection detection suite
  - Approval binding attack prevention

- **Platform Contracts** (phase1_platform_v2/schemas/)
  - AuthorityQueueItem.schema.json
  - InboxThread.schema.json

**Tasks Completed via Handoff:**
- ✅ TASK-029 to 032: Safety Gateway implementation
- ✅ TASK-036 to 040: DLP/PII Redaction
- ✅ TASK-041 to 044: Platform Contracts
- ✅ TASK-045 to 048: Receipt System
- ✅ TASK-049 to 052: Policy Gates (+ Policy Matrix)
- ✅ TASK-056 to 063: Testing Infrastructure

**Duration:** 1-2 weeks (deployment + integration testing)

### Additional Handoff Components

**GitHub PR Templates** (phase1_pr/):
- Pull request template (.github/pull_request_template.md)
- PR enforcement notes (docs/PHASE1_PR_ENFORCEMENT_NOTES.md)
- Use for Phase 1 code reviews

**CI/CD Workflows** (.github/workflows/):
- governance-gates.yml - Automated validation:
  - Policy matrix validation
  - Receipt integrity checks (TC-02)
  - Placeholder scanning
  - Secrets scanning
  - Evidence bundle validation

**Utility Scripts** (scripts/):
- CI scripts:
  - scan_placeholders.py (detect TODOs/FIXMEs)
  - scan_secrets.py (heuristic secret detection)  
  - validate_evidence_bundles.py (compliance validation)
- Evidence scripts:
  - export_evidence_bundle.py (SOC 2 evidence export)



---

### Track 1B: LangGraph Orchestrator + Ava (STILL NEED TO BUILD)

**Status:** ⏳ NOT STARTED

**Must Build:**
- LangGraph orchestrator (TASK-001 to 006)
- Ava AI personality (TASK-007 to 011)
- ARIS/ARS/AGCP research integrity (TASK-012 to 024)
- Uncertainty as first-class output (TASK-025 to 028)
- NeMo Guardrails integration (TASK-033 to 035)

**Duration:** 3-4 weeks (build from scratch)

---

### Parallel Execution Strategy

Both tracks can run simultaneously:
- **Track 1A:** Deploy gateway, run certification tests, integrate with database
- **Track 1B:** Build LangGraph orchestrator that calls Track 1A gateway

**Convergence:** Both tracks merge at end of Phase 1 - LangGraph orchestrator calls gateway for all tool executions.

**Total Phase 1 Time:** 3-4 weeks (vs original 5-6 weeks)

---

## Tasks

## Phase 1A: Substrate Validation (Week 3-5, 3 weeks)

**Objective:** Test and validate Trust Spine infrastructure deployed in Phase 0B

### 1. Receipt Coverage Audit (Week 3)

- [ ] **PHASE1A-TASK-001** Verify Receipt Generation for All Actions
  - Scan codebase for state-changing operations
  - Verify each operation calls `POST /v1/receipts`
  - Test: `receipt_coverage_test.py` → 100% coverage target
  - Missing receipts → add receipt generation code
  - **Verification:** 100% action logging (no silent execution)

### 2. Outbox Concurrency Testing (Week 3)

- [ ] **PHASE1A-TASK-002** Validate FOR UPDATE SKIP LOCKED
  - Create test: Simulate 10 concurrent workers claiming jobs
  - Verify no job claimed twice (100% isolation)
  - Test: `outbox_concurrency_test.py` → zero duplicate executions
  - **Verification:** Concurrency test passes

### 3. Idempotency Validation (Week 4)

- [ ] **PHASE1A-TASK-003** Test Duplicate Request Handling
  - Execute same outbox job 3 times with same idempotency_key
  - Verify only 1 execution occurs, others return cached receipt
  - Test: `idempotency_test.py` → 100% duplicate prevention
  - **Verification:** Cached receipts returned for duplicates

### 4. Policy Evaluation Integration (Week 4)

- [ ] **PHASE1A-TASK-004** Map Policy Decisions to Risk Tiers
  - Test policy-eval Edge Function with sample intents
  - Verify ALLOW/DENY/REQUIRE_APPROVAL decisions
  - Map: ALLOW=Green, REQUIRE_APPROVAL=Yellow, DENY=Red
  - Test: `policy_eval_test.py` → correct risk tiers
  - **Verification:** Policy eval returns correct risk tier 100%

### 5. RLS Isolation Testing (Week 5)

- [ ] **PHASE1A-TASK-005** Zero Cross-Tenant Leakage Validation
  - Create Suite A + Office A1, Suite B + Office B1
  - Test Suite A cannot read Suite B data
  - Test Office 1 cannot read Office 2 data (same Suite)
  - Test: `rls_isolation_test.py` → zero cross-tenant leakage
  - **Verification:** 100% tenant isolation (CRITICAL)

### 6. Hash Chain Verification (Week 5)

- [ ] **PHASE1A-TASK-006** Receipt Integrity Validation
  - Create 100 test receipts with previous_hash linkage
  - Verify hash chain via Go service `POST /v1/receipts/verify-run`
  - Test for broken links (must return 0 broken links)
  - **Verification:** Hash chain integrity 100% validated

---

## Phase 1B: Intelligence Layer Integration (Week 4-7, 4 weeks, PARALLEL WITH 1A)

**Objective:** Build Aspire LangGraph orchestrator + Safety Gateway + wire to Trust Spine Edge Functions

### 1. LangGraph Orchestrator Setup (Week 4)

- [ ] **PHASE1B-TASK-001** Install LangGraph + Agents SDK
  - Install: `pip install langgraph anthropic-agents`
  - Create orchestrator graph: Intake → Safety → Policy → Execute → Receipt
  - Wire orchestrator to Trust Spine Edge Functions (HTTP API calls)
  - Test orchestrator flow end-to-end (intent → receipt)
  - **Verification:** Orchestrator executes full flow

### 2. Safety Gateway Implementation (Week 5)

- [ ] **PHASE1B-TASK-002** Deploy NeMo Guardrails
  - Install NeMo Guardrails: `pip install nemoguardrails`
  - Configure jailbreak detection rules
  - Configure topic steering (business operations only)
  - Test: Jailbreak attempts blocked
  - **Verification:** NeMo test suite passes

- [ ] **PHASE1B-TASK-003** Deploy Presidio DLP
  - Install Presidio: `pip install presidio-analyzer presidio-anonymizer`
  - Configure PII redaction (SSN, CC, email → <REDACTED>)
  - Pattern: User Input → NeMo → Presidio → Trust Spine Inbox
  - Test: PII redacted in all receipts
  - **Verification:** Presidio validation passes

### 3. Capability Token Minting (Week 6)

- [ ] **PHASE1B-TASK-004** Implement Token Generation
  - Implement `mint_capability_token()` in orchestrator
  - Tokens: <60s expiry, suite_id + office_id + tool scoped
  - Signature: HMAC-SHA256
  - Edge Functions verify tokens before execution
  - Test: Invalid tokens rejected with 403 Forbidden
  - **Verification:** Token verification working

### 4. Receipt Integration (Week 6)

- [ ] **PHASE1B-TASK-005** Wire Orchestrator to Receipts API
  - Orchestrator calls `POST /v1/receipts` after every action
  - Store correlation_id from LangGraph trace
  - Verify receipt_hash computed correctly (SHA-256)
  - Test: Receipts created for 100% of actions
  - **Verification:** Receipt integration working

### 5. Platform Contracts Implementation (Week 7)

- [ ] **PHASE1B-TASK-006** Define API Specifications
  - Intent Ingest API specification (inbound → Aspire)
  - Capability Provider API specification (Aspire → external tools)
  - Receipt + Evidence API specification (audit trail)
  - Test: OpenAPI contract validated via Postman
  - **Verification:** Platform Contracts complete

### 6. Backend API Implementation (Week 7)

- [ ] **PHASE1B-TASK-007** Implement Mobile API Contracts
  - Session API (POST /api/sessions/start, GET /api/sessions/:id, WebSocket)
  - Authority Queue API (GET /api/authority-queue, POST /approve)
  - Receipt API (GET /api/receipts, POST /api/receipts, GET /verify-hash)
  - Capability Token API (POST /api/tokens/mint, POST /api/tokens/refresh)
  - Test: All mobile API contracts validated
  - **Verification:** Backend APIs match mobile contracts

### 7. Brain Layer Architecture Implementation (Week 6-7) - NEW

**Source:** `platform/brain/`

The Brain Layer is the intelligence hub of Aspire - it orchestrates routing, quality control, and state management.

#### QA Loop Implementation (Primary → Critic → Fix → Gate)

**Source:** `platform/brain/qa/`

- [ ] **PHASE1B-TASK-BL-001** Deploy 5 Critic Prompts
  - Deploy from `brain/qa/prompts/`:
    - `evidence_critic.prompt.md` - Validates evidence quality
    - `policy_critic.prompt.md` - Checks policy compliance
    - `quality_critic.prompt.md` - Ensures output quality
    - `safety_critic.prompt.md` - Validates safety constraints
    - `tool_plan_critic.prompt.md` - Reviews tool execution plans
  - Test: Each critic returns structured feedback
  - **Verification:** All 5 critics operational

- [ ] **PHASE1B-TASK-BL-002** Deploy 5 Evaluation Rubrics
  - Deploy from `brain/qa/rubrics/`:
    - Evidence rubric (v1)
    - Policy rubric (v1)
    - Quality rubric (v1)
    - Safety rubric (v1)
    - Tool plan rubric (v1)
  - Test: Rubrics score outputs correctly
  - **Verification:** Rubric evaluation working

- [ ] **PHASE1B-TASK-BL-003** Implement QA Loop Flow
  - Pattern: Primary agent drafts → Critics evaluate → Fix if fail → Gate check
  - If any critic fails → Primary must apply patch_plan and re-submit
  - Only after pass → submit to Authority Queue or Outbox
  - Test: Failed critic blocks submission
  - **Verification:** QA loop end-to-end working

#### Router Configuration

**Source:** `platform/brain/router/`

- [ ] **PHASE1B-TASK-BL-004** Deploy Router Policy
  - Deploy `brain/router/router_policy.yaml` (master routing rules)
  - Configure intent-to-skill-pack routing
  - Test: Intents route to correct skill packs
  - **Verification:** Router policy active

- [ ] **PHASE1B-TASK-BL-005** Deploy Desk-Specific Routers
  - Deploy from `brain/router/rules/`:
    - `adam_researchdesk_router.yaml` - Research intent routing
    - `finn_moneydesk_router.yaml` - Money movement routing
    - `milo_payrolldesk_router.yaml` - Payroll routing
    - `teressa_booksdesk_router.yaml` - Books/accounting routing
    - `sarah_frontdesk_router.yaml` - Front desk call routing
  - Test: Each desk router handles its domain correctly
  - **Verification:** All desk routers operational

- [ ] **PHASE1B-TASK-BL-006** Deploy Model Policies
  - Deploy from `brain/router/model_policies/`:
    - Primary reasoner selection rules
    - Fast/cheap classifier rules
    - Fallback chain configuration
  - Test: Correct model selected per task type
  - **Verification:** Model routing working

- [ ] **PHASE1B-TASK-BL-007** Deploy Tool Policies
  - Deploy from `brain/router/tool_policies/`:
    - Tool availability rules per risk tier
    - Tool parameter constraints
    - Tool timeout configurations
  - Test: Tool access controlled by policy
  - **Verification:** Tool policies enforced

#### State Machines (LangGraph Workflows)

**Source:** `platform/brain/state_machines/`

- [ ] **PHASE1B-TASK-BL-008** Deploy 6 LangGraph State Machines
  - Deploy from `brain/state_machines/`:
    - `conference_room.yaml` - Meeting/call orchestration
    - `inbox_triage.yaml` - Incoming item routing
    - `invoice_draft.yaml` - Invoice creation flow
    - `legal_contract_send.yaml` - Contract execution
    - `mail_ops_triage.yaml` - Email operations (admin)
    - `n8n_ops_triage.yaml` - Automation workflow triage
  - Convert YAML to LangGraph sub-graphs
  - Test: Each state machine executes correctly
  - **Verification:** All 6 state machines deployed

#### Validators & Evaluation

**Source:** `platform/brain/validators/`, `platform/brain/eval/`

- [ ] **PHASE1B-TASK-BL-009** Deploy Input/Output Validators
  - Deploy validators from `brain/validators/`:
    - Intent validation (required fields, format)
    - Output validation (schema compliance)
    - Receipt validation (hash integrity)
  - Test: Invalid inputs rejected
  - **Verification:** Validators operational

- [ ] **PHASE1B-TASK-BL-010** Deploy Evaluation Framework
  - Deploy from `brain/eval/`:
    - Test case definitions
    - Golden run recordings
    - Evaluation thresholds
  - Test: Evals can be executed programmatically
  - **Verification:** Eval framework ready

- [ ] **PHASE1B-TASK-BL-011** Configure LLM Call Infrastructure
  - Deploy from `brain/llm/call_llm.ts`:
    - Unified LLM call abstraction
    - Provider fallback logic
    - Token counting and budgeting
  - Test: LLM calls work through abstraction
  - **Verification:** LLM infrastructure working

#### Brain Layer Success Criteria

- [ ] `1B-SC-BL-001` All 5 critic prompts deployed and responding
- [ ] `1B-SC-BL-002` All 5 evaluation rubrics deployed
- [ ] `1B-SC-BL-003` QA loop blocks submissions that fail critic checks
- [ ] `1B-SC-BL-004` Router correctly routes 100% of test intents
- [ ] `1B-SC-BL-005` All 6 state machines execute correctly
- [ ] `1B-SC-BL-006` Validators reject invalid inputs
- [ ] `1B-SC-BL-007` Eval framework can run test suites

#### Brain Workflows (Evidence & Learning) - NEW

**Source:** `platform/brain/workflows/`

- [ ] **PHASE1B-TASK-BL-012** Deploy Evidence Pack Builder
  - Deploy `brain/workflows/evidence_pack_builder.ts`
  - Builds evidence packs for proposals (attachments, receipts, context)
  - Test: Evidence pack generated for proposal
  - **Verification:** Evidence packs attached to Authority Queue items

- [ ] **PHASE1B-TASK-BL-013** Deploy Learning Bundle Builder
  - Deploy `brain/workflows/learning_bundle_builder.workflow.ts`
  - Creates learning bundles from incidents (runbook, eval cases, robot assertions)
  - Test: Incident generates learning bundle
  - **Verification:** Learning bundles stored correctly

- [ ] **PHASE1B-TASK-BL-014** Deploy Legal Contract Bundle
  - Deploy `brain/workflows/legal_contract_bundle.workflow.ts`
  - Bundles legal contract artifacts (contract, signatures, evidence)
  - Test: Contract workflow produces bundle
  - **Verification:** Legal bundles complete

---

### 7B. Gateway Capability Policies (Week 7) - NEW

**Source:** `platform/gateway/policies/`

The Gateway enforces agent-specific capability policies that control what each skill pack can do.

#### Agent Capability Policies

- [ ] **PHASE1B-TASK-GW-001** Deploy Agent Capability Policies
  - Deploy from `gateway/policies/`:
    - `adam_capabilities.yaml` - Adam Research capabilities
    - `finn_capabilities.yaml` - Finn Money Desk capabilities
    - `milo_capabilities.yaml` - Milo Payroll capabilities
    - `nora_capabilities.yaml` - Nora Conference capabilities
    - `tec_capabilities.yaml` - Tec Documents capabilities
    - `teressa_capabilities.yaml` - Teressa Books capabilities
    - `council_capabilities.yaml` - Council (Meeting of Minds) capabilities
  - Test: Each agent restricted to defined capabilities
  - **Verification:** Capability enforcement working

#### System Policies

- [ ] **PHASE1B-TASK-GW-002** Deploy System Policies
  - Deploy from `gateway/policies/`:
    - `audio_limits.yaml` - Audio processing limits
    - `learning_data_policy.yaml` - Learning data handling rules
    - `multi_provider_data_policy.yaml` - Multi-provider data isolation
  - Test: System policies enforced
  - **Verification:** System limits active

- [ ] **PHASE1B-TASK-GW-003** Deploy Rate Limits and Redaction
  - Deploy from `gateway/policies/rate_limits/`:
    - `conference.yaml` - Conference rate limits
  - Deploy from `gateway/policies/redaction/`:
    - `conference.yaml` - Conference PII redaction rules
  - Test: Rate limits and redaction working
  - **Verification:** Gateway policies complete

---

### 7C. Platform Contracts & Schemas (Week 7) - NEW

**Source:** `platform/contracts/`

Platform contracts define the schema for all events, receipts, and data structures.

#### Receipt Schemas (20+ types)

- [ ] **PHASE1B-TASK-PC-001** Deploy Receipt Type Schemas
  - Deploy from `contracts/receipts/`:
    - `council.decision.schema.json` - Council decision receipts
    - `authority.item.approved.schema.json` - Approval receipts
    - `authority.item.denied.schema.json` - Denial receipts
    - `automation.workflow.*.schema.json` - Automation receipts
    - `document.*.schema.json` - Document operation receipts
    - `money.transfer.*.schema.json` - Money movement receipts
    - `payroll.*.schema.json` - Payroll operation receipts
    - `legal.contract.*.schema.json` - Legal operation receipts
  - Test: All receipt types validate against schemas
  - **Verification:** 20+ receipt schemas deployed

#### Event Schemas

- [ ] **PHASE1B-TASK-PC-002** Deploy Event Schemas
  - Deploy from `contracts/events/`:
    - `a2a.item.created.schema.json` - A2A task events
    - `model.route.selected.schema.json` - Model routing events
    - `outbox.job.completed.schema.json` - Outbox completion events
  - Test: Events validate against schemas
  - **Verification:** Event schemas deployed

#### Supporting Schemas

- [ ] **PHASE1B-TASK-PC-003** Deploy Supporting Schemas
  - Deploy from `contracts/`:
    - `capabilities/capability.schema.json` - Capability token schema
    - `evidence/evidence.pack.schema.json` - Evidence pack schema
    - `learning/*.schema.json` - Learning artifact schemas:
      - change_proposal, eval_case, incident_summary, robot_assertion, runbook
  - Test: All supporting schemas validate
  - **Verification:** Contract schemas complete

---

### 8. A2A Router/Flywheel Integration (Week 7-8) - NEW

**Source:** `platform/trust-spine/06_ADDONS/A2A_INBOX_V6/`

- [ ] **PHASE1B-TASK-A2A-001** Deploy A2A Migrations
  - Deploy 7 A2A migrations from `06_ADDONS/A2A_INBOX_V6/02_DB/migrations/`
  - Tables: `a2a_tasks`, `a2a_task_transitions`, `a2a_task_claims`
  - RLS policies for tenant isolation
  - Test: A2A tables created with correct schema
  - **Verification:** 7 A2A migrations applied successfully

- [ ] **PHASE1B-TASK-A2A-002** Deploy A2A Edge Functions
  - `a2a-inbox-enqueue`: Enqueue agent-to-agent task
  - `a2a-inbox-claim`: Worker claims task for execution
  - `a2a-inbox-transition`: Update task state (pending → claimed → completed/failed)
  - Test: All 3 Edge Functions responding
  - **Verification:** A2A Edge Functions deployed

- [ ] **PHASE1B-TASK-A2A-003** Wire A2A Worker Service
  - Implement A2A worker that polls `a2a_tasks` table
  - FOR UPDATE SKIP LOCKED claim pattern (same as outbox)
  - Route tasks to appropriate skill pack
  - Test: 10 concurrent A2A tasks claimed without duplicates
  - **Verification:** A2A worker concurrency test passes

- [ ] **PHASE1B-TASK-A2A-004** Agent-to-Agent Task Routing
  - Orchestrator can dispatch tasks to other agents
  - Pattern: Nora → routes to Eli for email follow-up, Quinn → routes to Teressa for books sync
  - All A2A tasks generate receipts
  - Test: Cross-skill-pack task routing
  - **Verification:** A2A routing end-to-end working

### 8. Control Plane Registry (Week 8) - NEW

**Source:** `platform/control-plane/registry/`

- [ ] **PHASE1B-TASK-CP-001** Skill Pack Registration System
  - Central registry for all skill packs (10 external from Ecosystem v12.7)
  - Store manifest, permissions, risk tiers
  - Enable/disable skill packs per suite
  - Test: Register all 10 skill packs (Sarah, Eli, Quinn, Nora, Adam, Tec, Finn, Milo, Teressa, Clara)
  - **Verification:** Registry contains all skill packs

- [ ] **PHASE1B-TASK-CP-002** Capability Discovery
  - API: `GET /v1/registry/capabilities`
  - Returns available skill packs for current suite
  - Filters by enabled status and permissions
  - Test: Capability discovery returns correct list
  - **Verification:** Capability discovery working

- [ ] **PHASE1B-TASK-CP-003** Deploy Agent Registry
  - Deploy 8 agent definitions from `registry/agents/`
  - Configure agent permissions and routing rules
  - Test: All agents registered and discoverable
  - **Verification:** Agent registry complete

- [ ] **PHASE1B-TASK-CP-004** Deploy Provider Registry
  - Deploy 4 provider registries from `registry/providers/`:
    - Gusto (payroll)
    - QuickBooks Online (accounting)
    - Moov (money movement)
    - Plaid (bank linking)
  - Test: Provider health checks working
  - **Verification:** Provider registry complete

- [ ] **PHASE1B-TASK-CP-005** Deploy Tool Definitions
  - Deploy 14 tool definitions from `registry/tools/`
  - Map tools to skill packs and permissions
  - Test: Tool discovery working
  - **Verification:** Tool registry complete

- [ ] **PHASE1B-TASK-CP-006** Deploy Permission Policies
  - Deploy from `policies/`:
    - `permissions.external.yaml` - External skill pack permissions
    - `permissions.internal.yaml` - Internal skill pack permissions
    - `permissions.legal.overlay.yaml` - Legal-specific overlays
    - `conference_defaults.yaml` - Conference settings
  - Test: Permission evaluation working
  - **Verification:** Policy enforcement active

- [ ] **PHASE1B-TASK-CP-007** Provider Health Monitoring
  - Deploy provider-control-center
  - Configure health checks (Gusto, QBO, Moov, Plaid)
  - Setup connection status dashboards
  - Test: Health check detects provider issues
  - **Verification:** Health monitoring active

- [ ] **PHASE1B-TASK-CP-008** Canary Rollout Configuration
  - Configure rollout policies from `rollouts/`
  - Deploy `internal_learning_canary.yaml`
  - Test: Canary deployment works for test change
  - **Verification:** Canary rollout operational

---

### 9. Mail/Domain Tool Registration (Week 7-8) — NEW (Phase 0C Integration)

**Prerequisite:** Phase 0C Domain Rail deployed on Railway

These tasks register the Domain Rail service as a tool in the orchestrator and define its governance scope.

- [ ] **PHASE1B-TASK-MAIL-001** Register Domain/Mail Tools in Orchestrator Catalog
  - Register `domain.check`, `domain.purchase`, `domain.dns.create`, `domain.dns.verify`, `domain.delete` tools
  - Register `mail.account.create`, `mail.account.delete` tools
  - Orchestrator routes `domain.*` and `mail.*` calls to Domain Rail service via S2S auth
  - Test: Intent "check if example.com is available" routes to Domain Rail
  - **Verification:** Tool catalog includes 7 mail/domain tools

- [ ] **PHASE1B-TASK-MAIL-002** Define Capability Token Scopes for Mail
  - New scopes: `domain.read`, `domain.write`, `domain.dns.write`, `mail.account.write`, `mail.account.read`
  - Scope validation in Domain Rail policy gate middleware
  - Test: Token with `domain.read` cannot call `domain.purchase` (scope mismatch → 403)
  - **Verification:** Scope enforcement working

- [ ] **PHASE1B-TASK-MAIL-003** Assign Risk Tiers for Mail Operations
  - GREEN: `domain.check`, `domain.dns.verify`, domain/mail reads
  - YELLOW: `domain.dns.create`, `mail.account.create`
  - RED: `domain.purchase`, `domain.delete`, `mail.account.delete`, `domain.transfer`
  - Add to policy matrix YAML
  - Test: Policy eval returns correct tier for each mail operation
  - **Verification:** Risk tiers enforced in policy evaluation

- [ ] **PHASE1B-TASK-MAIL-004** S2S Token Issuance for Domain Rail Calls
  - Orchestrator mints HMAC-based S2S tokens for Domain Rail requests
  - Token includes: suite_id, requested_action, expiry (<60s), signature
  - Domain Rail validates S2S token before processing
  - Test: Expired S2S token → 401, valid token → success
  - **Verification:** S2S token flow working end-to-end

#### Mail Tool Registration Success Criteria
- [ ] `1B-SC-MAIL-001` 7 mail/domain tools registered in orchestrator catalog
- [ ] `1B-SC-MAIL-002` Capability token scopes enforced for all mail operations
- [ ] `1B-SC-MAIL-003` Risk tiers correctly assigned (GREEN/YELLOW/RED)
- [ ] `1B-SC-MAIL-004` S2S token issuance and validation working

---

### 10. Architecture Decision Record (ADR) Cross-References (NEW)

**Source:** `platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/`

All Phase 1 implementations **MUST** comply with the following ADRs:

| ADR | Title | Key Requirements |
|-----|-------|------------------|
| **ADR-0001** | Canonical Identity | Suite/Office identity model, tenant isolation |
| **ADR-0002** | Receipts v1 | Receipt schema, hash chain, immutability |
| **ADR-0003** | Policy Contract | Policy evaluation gates, risk tier mapping |
| **ADR-0004** | Durable Execution Outbox | Outbox pattern for durability, idempotency |
| **ADR-0005** | Capability Tokens | Token minting, verification, expiry (<60s) |
| **ADR-0006** | Skill Pack Certification | Certification framework, TC-01/02/03 tests |
| **ADR-0007** | SLOs and Telemetry | SLO definitions, observability requirements |
| **ADR-0008** | Release Governance | 11 production gates, deployment checklist |

**Implementation Checkpoints:**

- [ ] **PHASE1B-ADR-001** ADR-0001 Compliance
  - Suite/Office identity model implemented
  - All queries use suite_id + office_id context
  - **Verification:** Identity model review complete

- [ ] **PHASE1B-ADR-002** ADR-0002 Compliance
  - Receipt schema matches specification
  - Hash chain linking working (previous_hash)
  - **Verification:** Receipt schema validated

- [ ] **PHASE1B-ADR-003** ADR-0003 Compliance
  - Policy evaluation returns correct verdicts
  - Risk tiers mapped correctly (Green/Yellow/Red)
  - **Verification:** Policy compliance review complete

- [ ] **PHASE1B-ADR-004** ADR-0004 Compliance
  - Outbox pattern implemented correctly
  - Idempotency keys enforced
  - **Verification:** Outbox pattern audit complete

- [ ] **PHASE1B-ADR-005** ADR-0005 Compliance
  - Tokens expire within 60 seconds
  - Signature verification working
  - **Verification:** Token security audit complete

---

### 11. CLAUDE JOB KIT Runbook Integration (NEW)

**Source:** `platform/trust-spine/08_CLAUDE_JOB_KIT/`

Phase 1 should reference and use the pre-built runbooks and templates:

**Phase 1 Resources:**
- **Runbooks:** `PHASE_1_TRUST_SPINE/RUNBOOKS/RUNBOOK.md` - Step-by-step deployment
- **Definition of Done:** `PHASE_1_TRUST_SPINE/DEFINITION_OF_DONE/` - Success gates
- **Prompts:** `PHASE_1_TRUST_SPINE/PROMPTS/` - Claude prompts for this phase
- **Templates:** `SHARED_TEMPLATES/` - Reusable templates

**Intelligence Integration Resources:**
- **Runbooks:** `PHASE_3_SKILL_PACKS_CERTIFICATION/RUNBOOKS/` - LangGraph wiring guide
- **Definition of Done:** `PHASE_3_SKILL_PACKS_CERTIFICATION/DEFINITION_OF_DONE/` - Integration gates

---

## Success Criteria

### Phase 1A Success Criteria (Substrate Validation)
- [ ] `1A-SC-001` Receipt coverage = 100% (all actions logged)
- [ ] `1A-SC-002` Outbox concurrency test passes (zero duplicate claims)
- [ ] `1A-SC-003` Idempotency test passes (100% duplicate prevention)
- [ ] `1A-SC-004` Policy evaluation returns correct risk tiers (Green/Yellow/Red)
- [ ] `1A-SC-005` RLS isolation test passes (100% zero cross-tenant leakage)
- [ ] `1A-SC-006` Hash chain verification passes (zero broken links)

### Phase 1B Success Criteria (Intelligence Integration)
- [ ] `1B-SC-001` LangGraph orchestrator wired to Trust Spine Edge Functions
- [ ] `1B-SC-002` Safety Gateway blocks jailbreak attempts (NeMo test suite passes)
- [ ] `1B-SC-003` PII redacted in all receipts (Presidio validation passes)
- [ ] `1B-SC-004` Capability tokens verified server-side (invalid token → 403)
- [ ] `1B-SC-005` Receipt integration working (correlation_id propagation confirmed)
- [ ] `1B-SC-006` Platform Contracts specification complete
- [ ] `1B-SC-007` Backend APIs match mobile contracts (Expokit TypeScript types)

### Phase 1B Success Criteria (A2A Router/Flywheel - NEW)
- [ ] `1B-SC-008` 7 A2A migrations deployed successfully
- [ ] `1B-SC-009` 3 A2A Edge Functions responding
- [ ] `1B-SC-010` A2A worker concurrency test passes (zero duplicate claims)
- [ ] `1B-SC-011` Cross-skill-pack task routing working end-to-end
- [ ] `1B-SC-012` Control Plane registry contains all 13 skill packs
- [ ] `1B-SC-013` Capability discovery returns correct skill pack list

### Memory System Success Criteria

- [ ] `1-MEM-001` 40+ Knowledge Graph entities (receipt patterns, governance rules)
- [ ] `1-MEM-002` 20+ skills/ changelog entries (accumulated wisdom)
- [ ] `1-MEM-003` 80% reduction in "I forgot to include correlationId" type mistakes
- [ ] `1-MEM-004` Zero cross-tenant data leakage in RLS tests (evil tests pass)

**Source:** `plan/00-success-criteria-index.md`

---

## Related Artifacts

**Created in This Phase:**
- LangGraph orchestrator (`backend/orchestrator/brain.py`)
- Ava integration (`backend/orchestrator/ava.py`)
- Receipt generation system (`backend/orchestrator/receipts.py`)
- Safety Gateway (NeMo Guardrails config)
- DLP/PII Redaction (Presidio integration)
- Platform Contracts (3 API specifications)
- Deployed schemas: `receipts.sql`, `checkpoints.sql`, `identity.sql`, `capability-tokens.sql`

**Used in Later Phases:**
- Phase 2: Orchestrator API (skill packs call through Brain)
- Phase 3: Orchestrator API (mobile app connects to Brain)
- Phase 4: Safety systems (hardening + evil tests)

---

## Related Gates

### Critical Gates Satisfied in This Phase

- **Gate 6: Receipts Immutable** (append-only, hash-chained, NO UPDATE/DELETE)
- **Gate 7: RLS Isolation** (multi-tenant security, 100% evil test pass rate)

### Other Gates Prepared

- Gate 8: Replay Demo (deterministic replay tested in this phase)
- Gate 9: SLO Dashboard (metrics collection started)
- Gate 10: Incident Runbooks (failure handling patterns established)

**Source:** `plan/gates/README.md`

---

## Estimated Duration

**Extended scope includes safety systems**

**Duration Breakdown:**
- Week 1-2: LangGraph + Ava + Receipt System (Core orchestrator)
- Week 3: ARIS + ARS + AGCP (Research integrity)
- Week 4: Safety Gateway + Guardrails + DLP/PII (Security systems)
- Week 5: Platform Contracts + Testing (Documentation + validation)
- Week 6: Buffer for troubleshooting + integration refinement

**Total:** 5-6 weeks

**Previous Estimate (Without Safety Systems):** 3-4 weeks
**Increase:** +2 weeks for safety systems
**Justification:** Trust/liability confidence increases 50% → 90%

---

## Cost

**$10-20/mo** - OpenAI API usage during development

**Breakdown:**
- OpenAI GPT-5: $10-15/mo (development usage, ~100k tokens/day)
- Exa/Perplexity (ARS Tier 1): $5/mo (development tier)
- All other infrastructure: $0/mo (using Phase 0B local setup)

---

## Notes

**Critical Safety Systems Added:**
1. Safety Gateway (NeMo Guardrails) - Prompt injection defense
2. Guardrails Layer - Safety/policy separation
3. DLP/PII Redaction (Presidio) - Compliance alignment

**Without These Systems:**
- Vulnerable to prompt injection attacks
- PII leakage into logs/receipts
- Cannot pass SOC 2/GDPR compliance
- Trust/liability confidence: 50%

**With These Systems:**
- Prompt injection blocked (100% evil tests pass)
- PII redacted (zero leakage)
- Compliance-ready (GDPR, CCPA, SOC 2 aligned)
- Trust/liability confidence: 90%

**Blocking Notes:**
- Phase 2 (Skill Packs) cannot start until orchestrator API working
- Phase 3 (Mobile App) cannot start until orchestrator API stable
- All future phases depend on receipts system (Gate 6)

---

## Related Files

- **Main Roadmap:** [Aspire-Production-Roadmap.md](../Aspire-Production-Roadmap.md)
- **Success Criteria:** [00-success-criteria-index.md](../00-success-criteria-index.md)
- **Dependencies:** [00-dependencies.md](../00-dependencies.md)
- **Previous Phase:** [phase-0b-tower-setup.md](phase-0b-tower-setup.md)
- **Next Phase:** [phase-2-founder-mvp.md](phase-2-founder-mvp.md)
- **Gates:** [../gates/gate-06-receipts-immutable.md](../gates/gate-06-receipts-immutable.md), [../gates/gate-07-rls-isolation.md](../gates/gate-07-rls-isolation.md)

---

**Last Updated:** 2026-02-12
**Status:** READY (Phase 0B complete — Trust Spine deployed, Desktop integrated, RLS verified)
**Parallel:** Phase 0C (Domain Rail) runs concurrently with Phase 1 ramp-up (no dependencies until mail tool registration in Week 7-8)
