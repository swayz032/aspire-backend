# Aspire Phase Dependencies Matrix

**Last Updated:** 2026-02-04 (Timeline v4.1 - ACCELERATED) | **Purpose:** Define explicit blocking relationships between phases

> **⚡ TIMELINE ACCELERATED**: Due to pre-built assets (Trust Spine, Ecosystem v12.7, Expokit, Admin Portal), remaining timeline is **24-28 weeks** (down from 52 weeks from-scratch estimate). **46% of work already complete!**

---

## 🗺️ Phase Execution Flow (ASCII Diagram)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      ASPIRE PRODUCTION PIPELINE                          │
│                        (Phases 0A → 6F)                                  │
└─────────────────────────────────────────────────────────────────────────┘

PHASE 0A: Laptop-Compatible Prep
   Duration: COMPLETE ✓ (was 2-3 days, accelerated via pre-built assets)
   Status: ✅ COMPLETE (2026-02-04)
   Blocking: None (can start immediately)
   │
   ├─ Deliverables:
   │  ├─ 7 cloud accounts (Supabase, OpenAI, Stripe, Upstash, S3, LiveKit, PolarisM)
   │  ├─ Repository initialized (monorepo structure)
   │  ├─ Core schemas designed (receipts.sql, checkpoints.sql, identity.sql)
   │  └─ 10 skill pack manifests designed (Ecosystem v12.7)
   │
   └──→ BLOCKS: Phase 0B
        ↓
        │
PHASE 0B: Skytech Tower Setup + Trust Spine Deploy
   Duration: 2-3 DAYS (hardware ready, just config needed)
   Status: ⏳ NOT STARTED
   Blocking: Phase 0A (needs cloud accounts + schemas ready)
   │
   ├─ Deliverables:
   │  ├─ WSL2 + Ubuntu 22.04 configured
   │  ├─ Postgres 16 + Redis 7 running locally
   │  ├─ CUDA Toolkit + Llama 3 inference operational
   │  ├─ n8n workflow engine accessible + 5 workflows deployed (NEW)
   │  ├─ Observability Stack foundation (OTEL, Grafana) (NEW)
   │  └─ Trust Spine migrations + Edge Functions deployed
   │
   └──→ BLOCKS: Phase 1
        ↓
        │
PHASE 1: Core Orchestrator + Safety Systems + Brain Layer
   Duration: 5-6 weeks (accelerated via Trust Spine pre-built substrate)
   Status: ⏳ NOT STARTED
   Blocking: Phase 0B (needs Postgres, Redis, local dev environment, n8n)
   │
   ├─ Deliverables:
   │  ├─ LangGraph orchestrator (Intent → Receipt flow)
   │  ├─ ARIS (Research Integrity System)
   │  ├─ ARS (Aspire Research System - 5 tiers)
   │  ├─ AGCP (Governed Cross-Platform Advice)
   │  ├─ Safety Gateway (NeMo Guardrails)
   │  ├─ Guardrails Layer (prompt injection defense)
   │  ├─ Presidio DLP (PII redaction)
   │  ├─ Receipt generation (100% coverage)
   │  ├─ Brain Layer (NEW):
   │  │  ├─ QA Loop (5 critics, 5 rubrics)
   │  │  ├─ Router Configuration (5 desk routers)
   │  │  ├─ State Machines (6 LangGraph workflows)
   │  │  └─ Validators & Evaluation framework
   │  ├─ Control Plane Registry (NEW):
   │  │  ├─ Agent registry (8 agents)
   │  │  ├─ Provider registry (4 providers)
   │  │  ├─ Skill pack registry
   │  │  └─ Permission policies
   │  ├─ A2A Router/Flywheel (7 migrations, 3 Edge Functions)
   │  └─ ADR Compliance (ADR-0001 through ADR-0008)
   │
   ├─ Gates Satisfied: 0, 1, 2, 3, 6, 7 (partial 8-10)
   │
   └──→ BLOCKS: Phase 2, Phase 3, Phase 4
        ↓
        │
PHASE 2: Founder Quarter MVP
   Duration: 8-10 weeks (accelerated via Ecosystem v12.7 scaffolds)
   Status: ⏳ NOT STARTED
   Blocking: Phase 1 (needs orchestrator + safety systems + Brain Layer operational)
   │
   ├─ Deliverables (11 Skill Packs):
   │  ├─ Channel Skill Packs (6):
   │  │  ├─ Sarah (Front Desk) - telephony, call routing
   │  │  ├─ Eli (Inbox) - PolarisM mail handling, triage
   │  │  ├─ Quinn (Invoicing) - Stripe Connect
   │  │  ├─ Nora (Conference) - LiveKit meetings
   │  │  ├─ Adam (Research) - Exa/Brave search
   │  │  └─ Tec (Documents) - PDF generation
   │  ├─ Finance Office Skill Packs (3):
   │  │  ├─ Finn (Money Desk) - Moov/Plaid transfers
   │  │  ├─ Milo (Payroll) - Gusto integration
   │  │  └─ Teressa (Books) - QuickBooks accounting
   │  ├─ Legal Skill Pack (1):
   │  │  └─ Clara (Legal) - PandaDoc contracts
   │  ├─ Internal Admin Skill Pack (1) (NEW):
   │  │  └─ mail_ops_desk - PolarisM admin (domain/mailbox management)
   │  ├─ Finance Office Supporting Systems (NEW):
   │  │  ├─ cash_buffer/ - Cash reserve forecasting
   │  │  ├─ reconciliation/ - Bank statement reconciliation
   │  │  ├─ accountant_mode/ - Read-only auditor interface
   │  │  └─ money_rules/ - Transfer policy configuration
   │  └─ Compliance: PII redaction, audit trails, bounded authority
   │
   ├─ Gates Satisfied: 0-9 (partial 10)
   │
   └──→ BLOCKS: Phase 3 (mobile needs orchestrator API)
        ↓
        │
PHASE 3: Mobile App
   Duration: 2-3 weeks (Expokit 43+ screens COMPLETE, just wiring needed)
   Status: ⏳ NOT STARTED
   Blocking: Phase 2 (needs orchestrator API + skill packs)
   │
   ├─ Deliverables:
   │  ├─ 6 UI Surfaces (Authority Dashboard, Inbox, Phone, Receipts, Intent Input, Call UI)
   │  ├─ LiveKit video integration (<2s latency)
   │  ├─ Degradation ladder (Video → Audio → Async → Text)
   │  ├─ 4-tab navigation (Inbox/Quarter/Workbench/Office, cold-start <2.5s)
   │  └─ Auto-downshift triggers (battery, network, latency)
   │
   ├─ Gates Satisfied: 0-10 (ALL gates complete)
   │
   └──→ BLOCKS: Phase 4 (hardening needs full v1 stack)
        ↓
        │
PHASE 4: Production Hardening
   Duration: 8-10 weeks (accelerated via ADR-0008 pre-built gates)
   Status: ⏳ NOT STARTED
   Blocking: Phase 3 (needs complete v1 infrastructure)
   │
   ├─ Deliverables:
   │  ├─ 10/10 Bundle (8 proof artifacts):
   │  │  ├─ 01. System Invariants (1-page constitution)
   │  │  ├─ 02. Threat Model (STRIDE analysis)
   │  │  ├─ 03. Capability Spec (token enforcement)
   │  │  ├─ 04. Observability Plan (SLO dashboard)
   │  │  ├─ 05. Incident Response (runbooks)
   │  │  ├─ 06. Isolation Enforcement (RLS tests)
   │  │  ├─ 07. Replay Protocol (deterministic audit)
   │  │  └─ 08. Evidence/Citations (source docs)
   │  ├─ Learning Loop (NEW): Incident-to-learning automation
   │  ├─ Quality Gate Workflow (NEW): Automated skill pack certification
   │  ├─ Observability Stack (NEW):
   │  │  ├─ OpenTelemetry instrumentation
   │  │  ├─ Alerting rules configuration
   │  │  ├─ Grafana dashboards
   │  │  └─ SLI/SLO metrics (ADR-0007)
   │  ├─ Trust Spine Addons Integration (NEW):
   │  │  ├─ BEHAVIOR_FLYWHEEL_V1
   │  │  ├─ OBSERVABILITY_OTEL_V1
   │  │  ├─ REPLAY_HARNESS_V1
   │  │  └─ 6 Operational Runbooks
   │  ├─ Internal Skill Packs (4) (NEW):
   │  │  ├─ sre_triage - SRE incident automation
   │  │  ├─ qa_evals - QA evaluation automation
   │  │  ├─ security_review - Security review automation
   │  │  └─ release_manager - Release management automation
   │  ├─ Pen testing (ZERO bypass vulnerabilities)
   │  ├─ RLS tests (100% pass rate)
   │  └─ Deterministic replay (100% of receipts)
   │
   ├─ Gates Satisfied: ALL (100% - production ready)
   │
   └──→ BLOCKS: Phase 5 (beta needs production-ready infrastructure)
        ↓
        │
PHASE 5: Beta Launch & Dogfooding
   Duration: 3 weeks (focused dogfooding period)
   Status: ⏳ NOT STARTED
   Blocking: Phase 4 (needs 10/10 Bundle + all gates passing)
   │
   ├─ Deliverables:
   │  ├─ 1,000+ receipts generated (dogfooding)
   │  ├─ 99% safety score achieved
   │  ├─ Chaos testing (zero critical failures)
   │  ├─ Internal team using Aspire daily
   │  └─ Positive feedback validation
   │
   └──→ BLOCKS: Phase 6 (scale requires stable v1)
        ↓
        │
PHASE 6: Scale & Expand
   Duration: 10-12 weeks (Meeting of Minds, multi-tenant scale)
   Status: ⏳ NOT STARTED
   Blocking: Phase 5 (needs v1 operational + dogfooding complete)
   │
   ├─ Sub-Phases:
   │  ├─ 6A: Hiring Assistant (~3 weeks)
   │  ├─ 6B: Tax & Compliance Assistant (~4 weeks)
   │  ├─ 6C: Meeting of Minds (multi-LLM council)
   │  ├─ 6D: Notary On-Demand (~2 weeks)
   │  ├─ 6E: Multi-Operator Architecture (~4 weeks)
   │  └─ 6F: Evolution Doctrine (~1 week)
   │
   └──→ ENABLES: Long-term viability, ecosystem growth


KEY:
──→  Blocking dependency (sequential)
├─   Delivers / Outputs
⏳   Not Started
🔄   In Progress
✅   Complete
❌   Blocked
```

---

## 📋 Dependency Table (Detailed)

| Phase | Blocking Phase | Blocks (Downstream) | Key Artifacts Required | Gates Satisfied |
|-------|----------------|---------------------|------------------------|-----------------|
| **0A** | None (can start immediately) | 0B | Cloud accounts, repo structure, schemas | None |
| **0B** | 0A | 1 | Postgres, Redis, WSL2, CUDA, n8n | None |
| **1** | 0B | 2, 3, 4 | LangGraph orchestrator, ARIS/ARS/AGCP, Safety Gateway, Receipts table | 0, 1, 2, 3, 6, 7 |
| **2** | 1 | 3 | 10 skill pack implementations (Ecosystem v12.7), Provider APIs | 0-9 (partial 10) |
| **3** | 2 | 4 | 6 UI surfaces, LiveKit mobile SDK, degradation ladder | 0-10 (ALL) |
| **4** | 3 | 5 | 10/10 Bundle (8 artifacts), pen test report, RLS test suite | ALL (100%) |
| **5** | 4 | 6 | 1000+ receipts, 99% safety score, chaos test results | ALL (validated) |
| **6** | 5 | Future phases | Multi-operator schema, Phase 2 skill packs, Evolution Doctrine | ALL (maintained) |

---

## 🔗 Artifact Dependencies (Cross-Phase)

### Receipts Table (`receipts.sql`)
- **Introduced:** Phase 0A (schema design)
- **Implemented:** Phase 1 (created in Postgres)
- **Validated:** Phase 1 (every action generates receipt)
- **Hardened:** Phase 4 (hash-chain integrity testing)
- **Tested:** Gate 6 (immutable + append-only)
- **Used:** Phases 1-6 (100% coverage)

### RLS Policies (`identity-rls.sql`)
- **Introduced:** Phase 0A (schema design)
- **Implemented:** Phase 1 (pg_policy creation)
- **Tested:** Phase 4 (evil tests - 100% cross-tenant blocking)
- **Verified:** Gate 7 (isolation enforcement)
- **Extended:** Phase 6E (multi-operator architecture)

### Skill Pack Manifests (JSON)
- **Designed:** Phase 0A (10 skill pack manifests from Ecosystem v12.7)
- **Implemented:** Phase 2 (10 skill packs operational)
- **Certified:** Phase 2 (bounded authority testing)
- **Maintained:** Phase 6 (scaling and multi-operator support)

### LangGraph Orchestrator (`brain.py`)
- **Scaffolded:** Phase 0A (optional skeleton)
- **Implemented:** Phase 1 (full Intent→Receipt flow)
- **Integrated:** Phase 2 (skill pack sub-graphs)
- **Tested:** Phase 4 (10/10 Bundle validation)
- **Scaled:** Phase 6E (multi-suite support)

### Safety Systems (NeMo, Guardrails, Presidio)
- **Implemented:** Phase 1 (all 3 systems operational)
- **Tested:** Phase 2 (skill pack certification)
- **Hardened:** Phase 4 (pen testing, prompt injection defense)
- **Validated:** Phase 5 (99% safety score in production)

---

## ⚠️ Critical Path (Longest Sequential Chain)

```
0A (DONE) → 0B (2-3d) → 1 (5-6w) → 2 (8-10w) → 3 (2-3w) → 4 (8-10w) → 5 (3w) → 6 (10-12w)
────────────────────────────────────────────────────────────────────────────
Total REMAINING Duration: ~24-28 weeks (~6 months)
Original FROM-SCRATCH estimate: 52 weeks
YOU SAVED: ~24 weeks (46% already done via pre-built assets!)

**⚠️ SYNC CHECK**: This timeline MUST match Aspire-Production-Roadmap.md v4.1
Run `npm run validate:timeline` to verify consistency.
```

**Note:** Some parallelization possible:
- Phase 3 (mobile UI) can start partial work while Phase 2 completes (requires orchestrator API ready)
- Phase 6 sub-phases can run in parallel if resources allow

---

## 🚨 Blocking Scenarios (What Stops Progress)

### Phase 1 Cannot Start Until:
- ✅ Phase 0B complete (Postgres + Redis running)
- ✅ Schemas deployed (`receipts.sql`, `checkpoints.sql`, `identity.sql`)
- ✅ Cloud accounts operational (OpenAI API, Supabase project)

### Phase 2 Cannot Start Until:
- ✅ Phase 1 complete (LangGraph orchestrator operational)
- ✅ Receipt generation working (100% coverage)
- ✅ Safety systems active (ARIS/ARS/AGCP, Safety Gateway, DLP)

### Phase 3 Cannot Start Until:
- ✅ Phase 2 complete (orchestrator API available)
- ✅ At least 1 skill pack operational (for mobile testing)
- ✅ LiveKit Cloud account configured

### Phase 4 Cannot Start Until:
- ✅ Phase 3 complete (full v1 stack assembled)
- ✅ All 10 gates implemented (not necessarily passing, but testable)
- ✅ Pen testing team available

### Phase 5 Cannot Start Until:
- ✅ Phase 4 complete (10/10 Bundle ready)
- ✅ ALL gates passing (no exceptions)
- ✅ Internal team onboarded for dogfooding

### Phase 6 Cannot Start Until:
- ✅ Phase 5 complete (v1 stable in production)
- ✅ 1000+ receipts generated (real usage data)
- ✅ 99% safety score achieved

---

## 🔄 Phase Transition Checklist

### Before Moving to Next Phase:
1. **Verify Success Criteria:** ALL checklist items ✅ in current phase
2. **Gates Check:** Confirm gates satisfied (if applicable)
3. **Artifact Validation:** Required artifacts available for next phase
4. **Documentation:** Update phase status in YAML front matter
5. **Knowledge Graph:** Store learnings (governance rules, debugging solutions)
6. **Reflection:** Generate proposals from `.claude/session-notes.md`
7. **Backup:** Commit changes with reflection receipt

---

**End of Dependency Matrix**
