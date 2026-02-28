# Aspire Production Roadmap v5.3

**Status:** Phase 0B COMPLETE | Next: Phase 0C (Domain Rail) + Phase 1 (LangGraph Orchestrator) in parallel
**Last Updated:** 2026-02-12
**Team:** Tonio (Founder/Product) + Claude Code (Main Dev)
**Mission:** Governed AI execution infrastructure where AI labor safely touches reality

---

## Timeline Overview

```
Phase 0:  ████████████████████ COMPLETE (Foundation Sync)
Phase 0A: ████████████████████ COMPLETE (Cloud accounts ready)
Phase 0B: ████████████████████ COMPLETE (Cloud 2026-02-10, Local Dev 2026-02-12)
Phase 0C: ░░░░░░░░░░░░░░░░░░░░ READY (Domain Rail, 1 week — parallel with Phase 1)
Phase 1:  ░░░░░░░░░░░░░░░░░░░░ READY (Orchestrator, 5-6 weeks)
Phase 2:  ░░░░░░░░░░░░░░░░░░░░ NOT STARTED (Skill Packs + Mail, 8-10 weeks)
Phase 3:  ░░░░░░░░░░░░░░░░░░░░ NOT STARTED (Desktop + Mobile, 2-3 weeks)
Phase 4:  ░░░░░░░░░░░░░░░░░░░░ NOT STARTED (Hardening + Mail Gates, 10 weeks)
Phase 5:  ░░░░░░░░░░░░░░░░░░░░ NOT STARTED (Beta, 3 weeks)
Phase 6:  ░░░░░░░░░░░░░░░░░░░░ NOT STARTED (Scale + White-Label Email, 12 weeks)
─────────────────────────────────────────────────────────
TOTAL REMAINING: ~28-34 weeks | SAVED: ~24 weeks (46%)
Phase 0C adds 0 net weeks (runs parallel with Phase 1 ramp-up)
```

---

## Phase Summary

| Phase | Duration | Status | Key Deliverable | Detail |
|-------|----------|--------|----------------|--------|
| **0** | 2-3 days | **COMPLETE** | Canonical schemas, lean roadmap, conflict resolution, layer sync | [phase-0-foundation-sync.md](phases/phase-0-foundation-sync.md) |
| **0A** | 1 day | **COMPLETE** | 19 cloud accounts, API keys, repository | [phase-0a-laptop-prep.md](phases/phase-0a-laptop-prep.md) |
| **0B** | 2-3 days | **COMPLETE** | Cloud: 49 migrations, 5 Edge Functions, 27/27 RLS. Local: WSL2, PG16, Redis, CUDA, Docker, n8n, observability | [phase-0b-tower-setup.md](phases/phase-0b-tower-setup.md) |
| **0C** | 1 week | Ready | Domain Rail on Railway (static IP), mail tables, ResellerClub client, S2S auth | [phase-0c-domain-rail.md](phases/phase-0c-domain-rail.md) |
| **1** | Week 3-8 | Ready | LangGraph orchestrator, receipts + hash chain, safety gateway, capability tokens, Ava contracts (AvaOrchestratorRequest/AvaResult), policy engine, mail tool registration | [phase-1-orchestrator.md](phases/phase-1-orchestrator.md) |
| **2** | Week 8-18 | Not Started | 11 skill packs + Eli mail expansion, OpsTelemetryFacade (6 endpoints), Admin Portal wiring, DLP redaction, Operator/Engineer toggle | [phase-2-founder-mvp.md](phases/phase-2-founder-mvp.md) |
| **3** | Week 19-21 | Not Started | Desktop (Railway) + Mobile (Expokit) integration, mail UI, governance enforcement | [phase-3-mobile-app.md](phases/phase-3-mobile-app.md) |
| **4** | Week 22-31 | Not Started | 11/11 Bundle, evil tests, SLO dashboard, mail/domains production gates | [phase-4-hardening.md](phases/phase-4-hardening.md) |
| **5** | Week 32-34 | Not Started | 1,000+ receipts, 99% safety, dogfooding | [phase-5-beta-launch.md](phases/phase-5-beta-launch.md) |
| **6** | Week 35-46 | Not Started | Cloud migration, Meeting of Minds, white-label email upgrade, customer beta | [phase-6-scale-expand.md](phases/phase-6-scale-expand.md) |
| **C** | Parallel | Not Started | Compliance (SOC 2 prep, PCI-DSS, HIPAA) | [phase-compliance-parallel.md](phases/phase-compliance-parallel.md) |

---

## Phase Transition Gates

| Transition | Must Pass | Verification |
|-----------|-----------|-------------|
| 0 -> 0B | All schemas/registries created, roadmap lean, conflicts tracked | Acceptance criteria in phase-0 file |
| 0A -> 0B | 19 cloud accounts operational, API keys verified | Changelog v4.1 |
| 0B -> 0C | Trust Spine deployed, Railway Pro active, ResellerClub API settings accessible | Static IP available |
| 0B -> 1 | Trust Spine canonical migrations deployed (see CANONICAL_PATHS.md), RLS verified, Edge Functions live | `rls_isolation_test.py` passes |
| 0C -> 2 | Domain Rail deployed, mail tables with RLS, ResellerClub API verified from Railway | Mail RLS isolation tests pass |
| 1 -> 2 | Gates 06, 07 satisfied, LangGraph E2E working, Ava contracts enforced | Receipt hash-chain verified, TC-01 through TC-07 pass |
| 2 -> 3 | All 10 skill packs operational, outbox integration working, OpsTelemetryFacade live | Certification TC-01/02/03, Admin Portal wired |
| 3 -> 4 | Mobile wired to backend, governance UI enforcing tiers | All 43+ screens with real data |
| 4 -> 5 | ALL 11 gates pass, 11/11 Bundle complete | Zero bypass vulnerabilities |
| 5 -> 6 | 1,000+ receipts, 99% safety, zero critical failures | Chaos testing passed |

---

## Gate Checklist

| Gate | Name | CLAUDE.md Category | Phase | Status |
|------|------|-------------------|-------|--------|
| 00 | Scope Lock | Product | 0A | DEFINED |
| 01 | UI Surface Invariants | Product | 3 | DEFINED |
| 02 | Call State Machine | Product | 3 | DEFINED |
| 03 | Forced Escalation | Security (Cat 5) | 1 | DEFINED |
| 04 | Degradation Ladder | Reliability (Cat 3) | 3 | DEFINED |
| 05 | Cold/Warm/Hot States | Operations (Cat 4) | 3 | DEFINED |
| 06 | Receipts Immutable | Testing (Cat 1) | 1 | IN PROGRESS (triggers + RLS deny policies active, hash-chain in Postgres) |
| 07 | RLS Isolation | Testing + Security (Cat 1+5) | 1 | IN PROGRESS (27/27 tests pass, FORCE RLS on 40 tables, dual-path RLS active) |
| 08 | Replay Demo | Testing (Cat 1) | 4 | DEFINED |
| 09 | SLO Dashboard | Observability (Cat 2) | 4 | DEFINED |
| 10 | Incident Runbooks | Operations (Cat 4) | 4 | DEFINED |

Gate definitions: [gates/](gates/) | Category mapping: [gate-reconciliation.md](gates/gate-reconciliation.md)

---

## Canonical Schemas (Single Source of Truth)

All naming conflicts resolved. These schemas generate SQL, TypeScript, and JSON definitions:

| Schema | File | Resolves |
|--------|------|----------|
| Risk Tiers | [risk-tiers.enum.yaml](schemas/risk-tiers.enum.yaml) | `green/yellow/red` (not low/medium/high) |
| Receipts | [receipts.schema.v1.yaml](schemas/receipts.schema.v1.yaml) | All 15+ CLAUDE.md mandatory fields |
| Capability Tokens | [capability-token.schema.v1.yaml](schemas/capability-token.schema.v1.yaml) | <60s expiry, HMAC-SHA256 |
| Tenant Identity | [tenant-identity.yaml](schemas/tenant-identity.yaml) | `suite_id` primary, `tenant_id` synced |
| Approval Status | [approval-status.enum.yaml](schemas/approval-status.enum.yaml) | pending/approved/rejected/expired/canceled |
| Outcome Status | [outcome-status.enum.yaml](schemas/outcome-status.enum.yaml) | success/denied/failed/timeout/pending |

### Ava Enterprise Contracts (Synced 2026-02-12)

Integrated from Ava Admin v2 (40 files) + Ava User v1.1 (31 files). All conflicts resolved ([CONFLICTS_RESOLVED.md](../docs/ava/CONFLICTS_RESOLVED.md)).

| Contract | File | Phase |
|----------|------|-------|
| AvaOrchestratorRequest | [ava_orchestrator_request.schema.json](contracts/ava-user/ava_orchestrator_request.schema.json) | 1A |
| AvaResult | [ava_result.schema.json](contracts/ava-user/ava_result.schema.json) | 1A (normalized: lowercase risk tiers) |
| ChangeProposal | [change_proposal.schema.json](contracts/ava-admin/change_proposal.schema.json) | 3+ |
| IncidentPacket | [incident_packet.schema.json](contracts/ava-admin/incident_packet.schema.json) | 3+ |
| OpsExceptionCard | [ops_exception_card.schema.json](contracts/ava-admin/ops_exception_card.schema.json) | 1B |
| OpsTelemetryFacade | [ops_telemetry_facade.openapi.yaml](contracts/ava-admin/ops_telemetry_facade.openapi.yaml) | 2 |

Specs: [plan/specs/ava-admin/](specs/ava-admin/) (6 files) | [plan/specs/ava-user/](specs/ava-user/) (9 files)

---

## Registries

| Registry | File | Purpose |
|----------|------|---------|
| Skill Packs | [skill-pack-registry.yaml](registries/skill-pack-registry.yaml) | 15 packs mapped (character, generic, APIs, risk tiers) |
| Phase Mapping | [phase-mapping.yaml](registries/phase-mapping.yaml) | MANIFEST A-F to Roadmap 0A-6 |
| Gate Satisfaction | [gate-satisfaction.yaml](registries/gate-satisfaction.yaml) | Gate status + evidence per phase |
| Conflict Registry | [conflict-registry.yaml](registries/conflict-registry.yaml) | 20 deep scan conflicts tracked |

---

## Platform Layer Architecture

4 core layers + Supabase state layer. 2,039 files in ecosystem zip. [Full asset index](reference/ecosystem-asset-index.md)

| Layer | What It Does | Phase | Readiness | Spec |
|-------|-------------|-------|-----------|------|
| **Control Plane** | Agent registry, skill packs, rollouts, certification | 2 | 40% | [control-plane-spec](reference/layer-specs/control-plane-spec.md) |
| **Brain** | LangGraph orchestrator (Single Brain), LLM router, QA loop | 1 | 10% | [brain-spec](reference/layer-specs/brain-spec.md) |
| **Gateway** | Safety enforcement, 35 tools, capability tokens, policy eval | 1 | 15% | [gateway-spec](reference/layer-specs/gateway-spec.md) |
| **Trust Spine** | Receipts, approvals, outbox, A2A, RLS (872 files) | 0B | 70% | [trust-spine-spec](reference/layer-specs/trust-spine-spec.md) |
| **Supabase** | Postgres + Edge Functions + Auth (state layer) | 0B | Ready | — |

**Cross-layer data flow:** [integration-map.md](reference/layer-specs/integration-map.md) | **Supporting layers:** [supporting-layers.md](reference/layer-specs/supporting-layers.md)

> **n8n** is automation plumbing (timers, retries, batch), NOT a platform layer. Stage: "soon" (not v1). It never decides (Law #1, #7).

---

## API-to-Phase Wiring

### Platform Contracts (Phase 1B)

**Intent Ingest API** (Inbound): Events enter via standardized schema with `risk_tier: green|yellow|red`
- See [ecosystem-architecture.md](reference/ecosystem-architecture.md) for full spec

**Capability Provider API** (Outbound): Tools called with signed capability tokens
- See [capability-token.schema.v1.yaml](schemas/capability-token.schema.v1.yaml) for token format

**Receipt + Evidence API** (Audit): All actions produce immutable receipts
- See [receipts.schema.v1.yaml](schemas/receipts.schema.v1.yaml) for canonical schema

### Backend APIs (Phase 1B, matching mobile contracts)
- Session API: `POST /api/sessions/start`, `GET /api/sessions/:id`, WebSocket
- Authority Queue: `GET /api/authority-queue`, `POST /api/authority-queue/:id/approve`
- Receipt API: `GET /api/receipts`, `POST /api/receipts`, `GET /api/receipts/:id/verify-hash`
- Capability Token: `POST /api/tokens/mint`, `POST /api/tokens/refresh`
- Staff Management: `GET /api/staff/available`, `POST /api/staff/enable`

---

## Phase Mapping (MANIFEST to Roadmap)

| MANIFEST | Roadmap | Focus |
|----------|---------|-------|
| Phase A | 0B + 1A | Trust Spine foundation, substrate validation |
| Phase B | 1B | Orchestrator, safety gateway |
| Phase C | 1B | Gateway boundary, platform contracts |
| Phase D | 2 (wk 15-20) | Provider integration |
| Phase E | 2 (wk 8-14) | Channel skill packs |
| Phase F | 3 | Mobile UI wiring |

Full mapping: [phase-mapping.yaml](registries/phase-mapping.yaml)

---

## Current Phase: Phase 0C (Domain Rail) + Phase 1 (Orchestrator) — Parallel

**Previous phase (0B Trust Spine Deploy):** COMPLETE (2026-02-10). 49 migrations applied to Supabase (42 core + 7 A2A), 5 Edge Functions deployed, Desktop server (`Aspire-Desktop/`) refactored to Trust Spine (15-column receipts, UUID suite_id, RLS middleware), PR #1 merged, www.aspireos.app live on Railway, 27/27 RLS isolation + evil tests pass, Stripe connected, all finance routes operational.

**What's next (parallel tracks):**

1. **Phase 0C — Domain Rail Foundation** (1 week): Stand up Domain Rail service on Railway (static IP for ResellerClub), create mail tables with full RLS, port S2S auth from handoff package. Infrastructure-only, no orchestration dependency. See [phase-0c-domain-rail.md](phases/phase-0c-domain-rail.md).

2. **Phase 1 — LangGraph Orchestrator** (5-6 weeks): Build the LangGraph "Brain" — Single Brain orchestrator (Law #1), capability token minting, approval flows, safety gateway (NeMo Guardrails + Presidio DLP), and the full execution pipeline connecting Intelligence Layer to Trust Spine. Now includes mail/domain tool registration + capability token scopes + Ava enterprise contracts. See [phase-1-orchestrator.md](phases/phase-1-orchestrator.md).

### Ava Integration Milestones (from Enterprise Sync)

**Phase 1A — Substrate Validation (Weeks 3-5):**
- Adopt AvaOrchestratorRequest as POST /v1/intents request schema
- Adopt AvaResult as response schema (normalized lowercase risk tiers)
- Implement receipt hash chain per `plan/specs/ava-admin/receipt_chain_spec.md`
- Add chain_id + sequence + prev_hash columns (Migration #50)
- Implement receipt chain verifier job (5-min interval)
- Adopt error code taxonomy (SCHEMA_VALIDATION_FAILED, APPROVAL_REQUIRED, etc.)
- Create policy_rules table (Migration #51)
- Implement POST /v1/policy/evaluate per `plan/specs/ava-user/policy_engine_spec.md`

**Phase 1B — Intelligence Integration (Weeks 4-7):**
- Wire capability token lifecycle per ava-user spec + canonical schema
- Implement approval binding with payload-hash integrity
- Implement presence session binding for RED tier
- Wire OpsExceptionCard emission for receipt chain violations

**Phase 2 — Founder Quarter MVP (Weeks 8-18):**
- Implement OpsTelemetryFacade (6 read-only endpoints) per `plan/contracts/ava-admin/ops_telemetry_facade.openapi.yaml`
- Wire Admin Portal to Telemetry Facade
- Implement Operator/Engineer toggle
- Implement DLP redaction per `plan/specs/ava-admin/dlp_redaction_matrix.md`
- Wire 4 initial skill packs through AvaOrchestratorRequest

**Phase 3+ — Certification:**
- ChangeProposal -> eval -> approval -> rollout pipeline
- IncidentPacket lifecycle + Learning Loop
- Insight Engine + Ritual Engine
- Research routing + n8n scheduling

Full sync manifest: [docs/ava/SYNC_MANIFEST.md](../docs/ava/SYNC_MANIFEST.md) | Integration README: [backend/orchestrator/ava/README.md](../backend/orchestrator/ava/README.md)

**Desktop app** (`Aspire-Desktop/`, Express + Expo web) is already deployed on Railway at www.aspireos.app. **Admin Portal** (`import-my-portal-main/`, Vite + React) is a UI prototype. **Mobile app** (Expokit, 43+ screens) will be integrated in Phase 3.

---

## Pre-Built Assets (Phase 0A Inventory)

- 19 API accounts verified and ready
- Trust Spine Ecosystem: Apply Trust Spine canonical migrations per MIGRATION_ORDER_ADDON.md (see CANONICAL_PATHS.md for exact paths and counts). Deploy 5 core Edge Functions + optional A2A addon (7 migrations + 3 Edge Functions).
- Ecosystem v12.7: All skill pack manifests
- Mobile Expokit: 43+ screens (React Native + Expo)
- Admin Portal: 170+ files prototype
- Hardware: Skytech Shadow (Ryzen 7 7700, RTX 5060, 32GB DDR5)
- Ava Enterprise Contracts: AvaOrchestratorRequest, AvaResult, ChangeProposal, IncidentPacket, OpsExceptionCard, OpsTelemetryFacade (synced 2026-02-12)
- Ava Specs: 15 runtime/governance specs across `plan/specs/ava-{admin,user}/`
- Ava Test Fixtures: Security negative cases, certification tests (TC-01 through TC-07), injection strings, example payloads

---

## Reference Documents

| Document | Content |
|----------|---------|
| [changelog.md](reference/changelog.md) | Roadmap version history (v1.1 through v4.2) |
| [worth-assessment.md](reference/worth-assessment.md) | Pre-implementation validation (88% confidence) |
| [technology-stack.md](reference/technology-stack.md) | Locked technology decisions |
| [ecosystem-architecture.md](reference/ecosystem-architecture.md) | Platform layers (4 core + 4 supporting), contracts, evolution doctrine |
| [triple-memory-strategy.md](reference/triple-memory-strategy.md) | Knowledge Graph + Serena + Session Reflection |
| [SYNC_MANIFEST.md](../docs/ava/SYNC_MANIFEST.md) | Ava Enterprise Sync manifest (42 files placed, 7 conflicts resolved) |
| [CONFLICTS_RESOLVED.md](../docs/ava/CONFLICTS_RESOLVED.md) | All 7 Ava sync conflict resolutions |

---

## Supporting Files

- **Master Index:** [plan/README.md](README.md)
- **Dependencies:** [00-dependencies.md](00-dependencies.md)
- **Success Criteria:** [00-success-criteria-index.md](00-success-criteria-index.md)
- **Gates Detail:** [gates/](gates/)
- **SQL Artifacts:** [artifacts/](artifacts/)
- **Skill Pack Specs:** [skill-packs/](skill-packs/)
- **Business Plan:** [Aspire_Business_Plan.md](Aspire_Business_Plan.md)

---

**END OF ROADMAP v5.3** | **Previous version:** v5.2 (2026-02-12) | **Archive:** [Aspire-Production-Roadmap-BACKUP-2026-01-10.md](archive/Aspire-Production-Roadmap-BACKUP-2026-01-10.md)

**v5.3 Changelog (2026-02-12):**
- Ava Enterprise Sync: Integrated Ava Admin v2 (40 files) + Ava User v1.1 (31 files) into canonical structure
- Added 6 Ava contracts to plan/contracts/ (AvaOrchestratorRequest, AvaResult, ChangeProposal, IncidentPacket, OpsExceptionCard, OpsTelemetryFacade)
- Added 15 Ava specs to plan/specs/ (receipt hash chain, DLP matrix, policy engine, approval binding, presence sessions, etc.)
- Updated Phase 1: Added Ava-specific milestones (1A substrate validation, 1B intelligence integration)
- Updated Phase 2: Added OpsTelemetryFacade, Admin Portal wiring, DLP redaction, Operator/Engineer toggle
- Updated Phase Transition Gates: 1->2 now requires Ava contract enforcement + TC-01..07
- Added Ava test fixtures (security negatives, certification tests, injection strings, example payloads)
- Created docs/ava/ with CONFLICTS_RESOLVED.md (7 conflicts) and SYNC_MANIFEST.md (42 files placed)
- Created backend/orchestrator/ava/README.md (Phase 1 implementation guide)
- Normalized ava_result.schema.json risk tiers: UPPERCASE -> lowercase (Conflict #1)

**v5.2 Changelog (2026-02-12):**
- Added Phase 0C: Domain Rail Foundation (PolarisMail + ResellerClub infrastructure)
- Updated Phase 1: Added mail/domain tool registration + capability token scopes
- Updated Phase 2: Expanded Eli Inbox with 13-state BYOD/Buy Domain machines, 16 mail receipt types
- Updated Phase 3: Renamed to "Desktop + Mobile" — Desktop (Railway) is primary, Mobile (Expokit) is secondary
- Updated Phase 4: Added mail/domains production gate checklist
- Updated Phase 6: Added white-label email upgrade milestone
- Clarified deployment architecture: Desktop on Railway (www.aspireos.app), Domain Rail on Railway (static IP), Admin Portal separate
