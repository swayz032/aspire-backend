# Ecosystem Asset Index (v12.7 — 2026-02-03)

**Purpose:** Complete inventory of the Aspire ecosystem zip mapped to plan docs, build phases, and readiness levels.
**Total:** 2,039 files across 16 top-level directories
**Zip Path Base:** `plan/temp_ecosystem_scan/aspire_ecosystem_v12.7_2026-02-03/`

---

## Readiness Legend

| Level | Meaning |
|-------|---------|
| **Implemented** | Code exists, tested or testable |
| **Scaffolded** | Stubs, schemas, or partial code — needs completion |
| **Designed** | Specs, docs, contracts — no code |
| **Placeholder** | Empty files or directory structure only |

---

## Platform Layer Inventory (1,267 files)

### Trust Spine — 872 files | Phase 0B | Readiness: Scaffolded (70%)

The largest and most complete layer. Contains migrations, edge functions, Go verifier, policy engine, and comprehensive test suites.

| Subdirectory | Files | Contents | Readiness |
|-------------|-------|----------|-----------|
| `00_START_HERE/` | ~5 | Bootstrap guide, deployment order | Designed |
| `01_ORIGINAL_INPUTS/` | ~10 | Source requirements, original specs | Designed |
| `02_CANONICAL/` | ~20 | Canonical schemas, ADR index | Designed |
| `03_SUPABASE_MIGRATIONS_ADDON/` | ~50 | 42 Trust Spine SQL migrations (tables, RLS, RPCs, indexes) | Implemented |
| `04_EDGE_FUNCTIONS/` | ~15 | 5 main edge functions (policy-eval, outbox-worker, outbox-executor, inbox, approval-events) | Scaffolded |
| `04_ROADMAP_REFERENCE/` | ~5 | Trust Spine ↔ Roadmap mapping | Designed |
| `05_CI_TEMPLATES/` | ~5 | CI/CD pipeline templates | Scaffolded |
| `05_SYNC_REPORT/` | ~3 | Cross-artifact sync validation | Designed |
| `06_ADDONS/` | ~100 | A2A Inbox V6 (7 migrations, 3 edge functions), Replay Harness, Executor Node, Observability OTEL, Behavior Flywheel | Scaffolded |
| `07_CI/` | ~5 | Continuous integration configs | Scaffolded |
| `08_CLAUDE_JOB_KIT/` | ~10 | Claude Code automation scripts | Designed |
| `09_RECEIPTS/` | ~15 | Receipt system: schemas, verification, hash-chain | Scaffolded |
| `10_POLICY_ENGINE/` | ~15 | Policy evaluation: schemas, RPC, default rules, matching | Scaffolded |
| `11_EXECUTOR/` | ~10 | Outbox executor: worker, claim RPC, delivery guarantees | Scaffolded |
| `12_CERTIFICATION/` | ~10 | Skill pack certification (TC-01/02/03) | Designed |
| `13_E2E_TESTS/` | ~15 | End-to-end test scenarios | Designed |
| `14_STRESS_TESTS/` | ~10 | Load testing specs | Designed |
| `authority-queue/` | ~20 | Authority Queue API, approval flow | Scaffolded |
| `CLAUDE_BOOTSTRAP/` | ~10 | Bootstrap automation for Claude | Designed |
| `db/` | ~50 | Database schemas, seed data | Scaffolded |
| `policies/` | ~15 | Default policy definitions | Designed |
| `policy_engine/` | ~20 | Policy eval implementation stubs | Scaffolded |
| `receipts/` | ~20 | Receipt generation, hashing, verification | Scaffolded |
| `schemas/` | ~15 | TypeScript/SQL type definitions | Scaffolded |
| `security/` | ~10 | Security policies, key management | Designed |

**Plan References:**
- `plan/reference/layer-specs/trust-spine-spec.md` — Bridge specification (architecture, Law mapping)
- `plan/CANONICAL_PATHS.md` — File navigation guide (canonical path mapping)
- `plan/SYNC-AUDIT-REPORT.md` — Sync verification results
- `plan/schemas/receipts.schema.v1.yaml` — Canonical receipt schema
- `plan/schemas/capability-token.schema.v1.yaml` — Token schema
- `plan/phases/phase-0b-tower-setup.md` — Deployment plan

---

### Brain — 52 files | Phase 1 | Readiness: Designed (10%)

Agent personas, state machines, LLM router, QA loop — richest spec layer but zero LangGraph implementation.

| Subdirectory | Files | Contents | Readiness |
|-------------|-------|----------|-----------|
| `eval/` | ~8 | Eval harness: test fixtures (conference, council, learning), threshold defs | Designed |
| `graphs/` | ~5 | LangGraph graph definitions (stubs) | Placeholder |
| `indexers/` | ~3 | Vector indexing configs | Designed |
| `llm/` | ~8 | LLM router policy: tier definitions (FAST/PRIMARY/HIGH_RISK), per-agent routing, fallback chains | Designed |
| `qa/` | ~10 | QA Loop: 5 critics (evidence, policy, quality, safety, tool_plan) with prompts + rubrics | Designed |
| `router/` | ~5 | Intent routing logic | Designed |
| `state_machines/` | ~5 | 5 YAML workflows (invoice_draft, inbox_triage, legal_contract, conference_room, mail_ops) | Designed |
| `validators/` | ~3 | Output validators | Designed |
| `workflows/` | ~5 | quality_gate.workflow.ts and others | Scaffolded |

**Plan References:**
- `plan/registries/skill-pack-registry.yaml` — Agent names + risk tiers
- `plan/phases/phase-1-orchestrator.md` — Build plan
- **NEW:** `plan/reference/layer-specs/brain-spec.md` — Bridge specification

---

### Gateway — 59 files | Phase 1 | Readiness: Designed (15%)

Comprehensive policy definitions and safety guards. Critical enforcement boundary.

| Subdirectory | Files | Contents | Readiness |
|-------------|-------|----------|-----------|
| `docs/` | ~8 | POLICY_WIRING.md (core flow), WEBHOOKS.md (standards), architecture docs | Designed |
| `enforcement/` | ~8 | Enforcement engine stubs | Scaffolded |
| `policies/` | ~15 | Per-agent capability YAMLs (5 agents), tools_catalog.yaml, tool allowlists | Designed |
| `safety/` | ~8 | 3 TypeScript guards: video_presence, high_risk_interrupt, recording_consent | Scaffolded |
| `security/` | ~5 | PII redaction: redaction.ts, data_minimizer.ts, conference redaction rules | Scaffolded |
| `src/` | ~8 | Gateway source stubs | Scaffolded |
| `tests/` | ~7 | Gateway test stubs | Placeholder |

**Plan References:**
- `plan/schemas/capability-token.schema.v1.yaml` — Token validation
- `plan/schemas/risk-tiers.enum.yaml` — Risk tier definitions
- **NEW:** `plan/reference/layer-specs/gateway-spec.md` — Bridge specification

---

### Control Plane — 75 files | Phase 2 | Readiness: Scaffolded (40%)

Registry, rollouts, certification system. Most mature supporting-layer infrastructure.

| Subdirectory | Files | Contents | Readiness |
|-------------|-------|----------|-----------|
| `api/` | ~3 | openapi.yaml: REST API for registry, rollouts, state transitions | Designed |
| `approvals/` | ~5 | Approval workflow integration | Designed |
| `config/` | ~3 | Configuration management | Designed |
| `db/migrations/` | ~10 | 8 SQL migrations (registry_items, rollouts, pgvector, eval_runs, incidents, legal, meetings) | Implemented |
| `docs/` | ~8 | 5 docs: overview, state machine, rollout/canary, receipts/audit, Nora admin | Designed |
| `policies/` | ~5 | Permission policies: external, internal, legal overlay, conference defaults | Designed |
| `provider-control-center/` | ~5 | Gusto connection, health checks, money movement health | Designed |
| `registry/` | ~5 | Agent registry: n8n_ops_desk placeholder, structure | Placeholder |
| `rollouts/` | ~5 | Rollout execution logic | Designed |
| `schemas/` | ~5 | Control plane schemas | Designed |
| `src/` | ~8 | Source stubs | Scaffolded |
| `CLAUDE_HANDOFF_README.md` | 1 | Implementation handoff instructions | Designed |

**Runbooks (7, in `platform/runbooks/`):**
- duplicate_transfer_prevention.md
- gusto_token_refresh_failures.md
- gusto_webhook_failures.md
- payroll_submit_failed.md
- provider_outage_mode.md
- transfer_stuck_pending.md
- N8N_WORKFLOW_OPERATIONS.md

**Plan References:**
- `plan/registries/skill-pack-registry.yaml` — Skill pack definitions
- `plan/registries/gate-satisfaction.yaml` — Gate tracking
- **NEW:** `plan/reference/layer-specs/control-plane-spec.md` — Bridge specification

---

### Contracts — 59 files | Phase 1 | Readiness: Designed (20%)

Shared binding interfaces that all layers reference.

| Subdirectory | Files | Contents | Readiness |
|-------------|-------|----------|-----------|
| `capabilities/` | ~10 | Capability token contracts, scope definitions | Designed |
| `events/` | ~12 | Event schemas: intent, approval, receipt, state-change | Designed |
| `evidence/` | ~8 | Evidence capture contracts | Designed |
| `learning/` | ~10 | Learning loop contracts (behavior flywheel) | Designed |
| `providers/` | ~10 | Provider adapter interface contracts | Designed |
| `receipts/` | ~9 | Receipt format contracts | Designed |

**Plan References:**
- `plan/schemas/receipts.schema.v1.yaml` — Receipt contract canonical
- `plan/schemas/capability-token.schema.v1.yaml` — Capability contract canonical

---

### Finance Office — 42 files | Phase 2 | Readiness: Designed (25%)

Domain-specific financial operations module.

| Subdirectory | Files | Contents | Readiness |
|-------------|-------|----------|-----------|
| `accountant_mode/` | ~5 | External accountant role, permissions, session schema | Designed |
| `books/` | ~8 | Accounting: categorization rules, vendor matching, snapshot builders | Designed |
| `cash_buffer/` | ~5 | Cash buffer policy, risk exceptions | Designed |
| `evidence/` | ~5 | Mobile receipt capture, matching rules, receipt inbox | Designed |
| `micro_lessons/` | ~3 | Financial literacy content | Designed |
| `money_rules/` | ~5 | Business rules for money operations | Designed |
| `money-movement/` | ~5 | Transfer operations, Moov/Plaid integration specs | Designed |
| `payroll/` | ~3 | Gusto payroll integration specs | Designed |
| `reconciliation/` | ~3 | Bank reconciliation logic | Designed |

**Plan References:** Currently unreferenced in plan folder.

---

### Providers — 63 files (platform) + 232 files (top-level) | Phase 2 | Readiness: Mixed

| Provider | Platform Files | Top-Level Files | Readiness |
|----------|---------------|-----------------|-----------|
| **Gusto** | ~15 | ~60 | Scaffolded (OAuth, payroll CRUD, replay/dedup) |
| **Moov** | ~10 | ~45 | Designed (transfer specs, ACH, wallets) |
| **Plaid** | ~10 | ~40 | Designed (connection, transactions, identity) |
| **QuickBooks (QBO)** | ~10 | ~40 | Designed (accounting sync, invoicing) |
| **Square** | ~8 | ~25 | Designed (POS, payments) |
| **Stripe** | ~10 | ~22 | Designed (invoicing, payments, customers) |

**Fixtures (test data):** `platform/fixtures/` — gusto, moov, plaid, qbo

**Plan References:**
- `plan/reference/technology-stack.md` — Lists API partners (names only)
- `plan/registries/skill-pack-registry.yaml` — Maps providers to skill packs

---

### Observability — 7 files | Phase 4 (specs used from Phase 1) | Readiness: Designed

| File | Contents |
|------|----------|
| `README.md` | Observability strategy: translate receipts into metrics |
| `metrics/metrics_definitions.md` | Metric specs: provider health, ingestion rate, approval time, exception rate |
| `alerts/` | Alert definitions and thresholds |
| `PII_REDACTION.md` | Log redaction policies |

**Plan References:** Gate 09 mention in roadmap.

---

### Services — 7 files | Phase 2 | Readiness: Designed

| File | Contents |
|------|----------|
| `mail-gateway/ARCHITECTURE.md` | Mail gateway service design |
| `mail-gateway/api_contract.inbox_v2.md` | Inbox API contract |
| `mail-gateway/inbox_sync_worker.spec.md` | IMAP sync (scheduled by n8n) |
| `mail-gateway/outbox_smtp_executor.spec.md` | SMTP sending |
| `mail-gateway/credential_vault.spec.md` | Email credential storage |
| `mail-gateway/attachment_pipeline.spec.md` | File handling pipeline |

**Plan References:** Currently unreferenced in plan folder.

---

### n8n Integration — 10 files | Phase "soon" (NOT v1) | Readiness: Placeholder (5%)

| File | Contents |
|------|----------|
| `README.md` | "n8n internal orchestration only" |
| `SECURITY_MODEL.md` | "Request only model" — no autonomous decisions |
| `MCP_CLAUDE_WORKFLOW.md` | Claude ↔ n8n MCP bridge |
| `SETUP_SELF_HOSTED.md` | Docker-based n8n setup |
| `templates/WORKFLOW_HARDENING_CHECKLIST.md` | Ops hardening checklist |
| `templates/workflows/finance/FIN_DAILY_SYNC.json` | Empty shell `{}` |
| `templates/workflows/mail/MAIL_*.json` (4 files) | Empty shells |

**Plan References:** ecosystem-architecture.md n8n clarification section.

---

### Remaining Supporting Layers

| Layer | Files | Contents | Phase | Readiness |
|-------|-------|----------|-------|-----------|
| **Trust Center** | 9 | Partner compliance templates, security questionnaire | 5 | Designed |
| **Security** | 5 | SECRETS_AND_ENVIRONMENTS.md, security posture docs | 0B+ | Designed |
| **Ingest** | 3 | Intent API schema, replay harness | 1 | Designed |
| **Retention** | 3 | GDPR deletion + export workflows | 4 | Designed |
| **Billing** | 1 | USAGE_METERING_INTEGRATION.md | 3 | Designed |
| **Storage** | ~3 | S3 blob storage patterns | 0B | Designed |
| **QA/Robots** | ~5 | Automated QA test robots | 4 | Designed |
| **Admin** | ~8 | Admin portal contracts + docs | 3 | Designed |
| **Dev** | ~3 | Development environment configs | 0B | Designed |

---

## Non-Platform Inventory (772 files)

### Agent Kits — 106 files | Phase 1-2 | Readiness: Designed

Agent persona definitions with system prompts, constraints, tool allowlists, fewshot examples, and governance blocks.

| Component | Contents |
|-----------|----------|
| **Shared Foundation** | 7 governance blocks: system_base, governance, tool_rules, receipt_rules, escalation_rules, style_guide, output_schema |
| **Agent Personas** | 10+: Ava (orchestrator), Sarah (front desk), Adam (research), Finn (money), Milo (payroll), Teressa (books), Eli (inbox), Quinn (invoicing), Nora (conference), TEC (documents), Clara (legal) |
| **Per Agent** | system_prompt.md, constraints.yaml, tools.yaml, fewshots/, style_guide.md |

**Plan References:** `plan/registries/skill-pack-registry.yaml` (names + risk tiers only)

---

### Skill Packs — 112 files | Phase 2 | Readiness: Designed

Full skill pack manifest structure with adapters, policies, and prompts.

| Component | Contents |
|-----------|----------|
| **Manifests** | Per-pack: manifest.json, persona/, adapters/, policies/, prompts/ |
| **Adapters** | authority_queue.adapter.ts, evidence_store.adapter.ts, tool_proxy.adapter.ts, handoff adapters |
| **Policies** | risk_policy.yaml, tool_policy.yaml, sources_policy.yaml, freshness_policy.yaml |
| **Prompts** | Task-specific prompts per skill pack |

**Plan References:** `plan/registries/skill-pack-registry.yaml`, `plan/skill-packs/` directory

---

### Docs — 31 files | Reference | Readiness: Designed

| Key File | Contents |
|----------|----------|
| `Aspire_Ecosystem_vNext.md` | Master ecosystem architecture document |
| `BRIDGE_TO_ROUTER_FLYWHEEL.md` | Learning loop: coordination → cost/quality → learning layers |
| Various ADRs and architectural docs | Design decisions, integration guides |

---

### Handoff — 15 files | Reference | Readiness: Designed

Claude Handoff package: implementation instructions, bootstrap scripts, deployment guides.

Includes:
- `02_MEETING_OF_MINDS.md` — Multi-agent council architecture
- `03_LEARNING_LOOP.md` — Behavior flywheel system

---

### Top-Level Providers — 232 files | Phase 2 | Readiness: Mixed

Detailed provider integration packages (separate from platform/providers/):
- Full API client specs per provider
- Authentication flows (OAuth 2.0, API keys)
- Data mapping to canonical schemas
- Error handling patterns

---

### Reference — 126 files | Cross-Phase | Readiness: Designed

Architecture references, decision records, integration guides.

---

### Other Directories

| Directory | Files | Contents |
|-----------|-------|----------|
| `RUNBOOKS/` | 5 | Operational runbooks (provider outages, deployment) |
| `scripts/` | 6 | Automation scripts |
| `ui_handoff/` | 9 | Mobile UI handoff specs |
| `config/` | 3 | Global configuration |
| `DNS_TEMPLATES/` | 4 | DNS configuration templates |
| `ENVIRONMENTS/` | 4 | Environment definitions (dev, staging, prod) |
| `00_START_HERE/` | 1 | Getting started guide |
| `.github/` | 2 | GitHub Actions workflows |

---

## Coverage Summary

| Category | Ecosystem Files | Plan Coverage | Gap |
|----------|----------------|---------------|-----|
| Trust Spine | 872 | Referenced (3 docs + schemas) | **Layer spec needed** |
| Brain | 52 | Minimal (registry names only) | **Layer spec needed** |
| Gateway | 59 | Minimal (token schema only) | **Layer spec needed** |
| Control Plane | 75 | Partial (skill pack registry) | **Layer spec needed** |
| Contracts | 59 | Partial (receipt + token schemas) | Layer spec captures |
| Finance Office | 42 | None | Layer spec captures |
| Providers | 295 (combined) | Names only (tech stack) | Layer spec captures |
| Agent Kits | 106 | Names only (skill pack registry) | Brain spec captures |
| Skill Packs | 112 | Names only (skill pack registry) | Control plane spec captures |
| Observability | 7 | Gate reference only | Layer spec captures |
| Other (12 dirs) | 360 | Minimal | Supporting layers spec |
| **Total** | **2,039** | **~15% referenced** | **Layer specs will bring to ~90%** |

---

**End of Ecosystem Asset Index**
