# Plan Folder Onboarding

**Purpose:** Understand the plan folder's structure, conventions, and navigation patterns.
**Last Updated:** 2026-02-07 | **Current Phase:** 0B (Trust Spine Deploy)

---

## What This Folder Is

The `plan/` folder is the **single source of truth** for Aspire's production roadmap, specifications, schemas, and governance gates. It bridges two worlds:

1. **The Roadmap** — What we're building, in what order, with what gates
2. **The Ecosystem ZIP** (v12.7, 2,039 files) — Pre-built specifications, migrations, and tooling

The plan folder **references** the ecosystem zip's detailed specs through bridge documents (layer specs). It does NOT duplicate them.

---

## Folder Structure

```
plan/
├── Aspire-Production-Roadmap.md    ← START HERE: Lean roadmap (<200 lines)
├── README.md                        ← Navigation index (all links)
├── ONBOARDING.md                    ← You are here
├── Aspire_Business_Plan.md          ← Business model, ICP, revenue
│
├── phases/                          ← Phase-by-phase execution plans
│   ├── phase-0-foundation-sync.md   ← COMPLETE
│   ├── phase-0a-laptop-prep.md      ← COMPLETE
│   ├── phase-0b-tower-setup.md      ← NEXT (Trust Spine deploy)
│   ├── phase-1-orchestrator.md      ← Brain + Gateway build
│   ├── phase-2-founder-mvp.md       ← 10 Skill Packs
│   ├── phase-3-mobile-app.md        ← 43+ screens, LiveKit
│   ├── phase-4-hardening.md         ← 10/10 Bundle, evil tests
│   ├── phase-5-beta-launch.md       ← 1,000+ receipts, dogfooding
│   ├── phase-6-scale-expand.md      ← Cloud migration, marketplace
│   └── phase-compliance-parallel.md ← SOC 2, PCI-DSS, HIPAA
│
├── schemas/                         ← Canonical data definitions (YAML)
│   ├── risk-tiers.enum.yaml         ← green/yellow/red (Law #4)
│   ├── receipts.schema.v1.yaml      ← 15+ mandatory fields (Law #2)
│   ├── capability-token.schema.v1.yaml ← <60s, HMAC-SHA256 (Law #5)
│   ├── tenant-identity.yaml         ← suite_id canonical (Law #6)
│   ├── approval-status.enum.yaml    ← pending/approved/rejected/expired/canceled
│   └── outcome-status.enum.yaml     ← success/denied/failed/timeout/pending
│
├── registries/                      ← Cross-reference lookups
│   ├── skill-pack-registry.yaml     ← 15 packs: character, APIs, risk tiers
│   ├── phase-mapping.yaml           ← MANIFEST A-F → Roadmap 0A-6
│   ├── gate-satisfaction.yaml       ← Gate status + evidence per phase
│   └── conflict-registry.yaml       ← 20 conflicts with resolutions
│
├── gates/                           ← Production readiness requirements
│   ├── README.md                    ← Gates overview
│   ├── gate-reconciliation.md       ← Category mapping
│   └── gate-00 through gate-10      ← Individual gate definitions
│
├── artifacts/                       ← Generated SQL schemas
│   ├── receipts-schema.sql          ← Receipt table DDL
│   ├── capability-tokens-schema.sql ← Token table DDL
│   ├── checkpoints-schema.sql       ← LangGraph checkpoint DDL
│   └── identity-rls-schema.sql      ← Identity + RLS policies
│
├── reference/                       ← Architecture docs + bridge specs
│   ├── agent-onboarding.md          ← Agent quick-start guide
│   ├── ecosystem-asset-index.md     ← Full 2,039 file inventory
│   ├── ecosystem-architecture.md    ← Platform layers, contracts, doctrine
│   ├── technology-stack.md          ← Locked tech decisions
│   ├── changelog.md                 ← Roadmap version history
│   ├── worth-assessment.md          ← Pre-implementation validation (88%)
│   ├── triple-memory-strategy.md    ← KG + Serena + Session Reflection
│   │
│   └── layer-specs/                 ← Ecosystem ZIP bridge documents
│       ├── trust-spine-spec.md      ← 872 files: migrations, edge functions, Go verifier
│       ├── brain-spec.md            ← 52 files: personas, state machines, QA, router
│       ├── gateway-spec.md          ← 59 files: policies, 35 tools, safety guards
│       ├── control-plane-spec.md    ← 75 files: registry, rollouts, certification
│       ├── supporting-layers.md     ← n8n, observability, providers, services, etc.
│       └── integration-map.md       ← Cross-layer data flow + worked examples
│
├── skill-packs/                     ← Individual skill pack specifications
├── 00-dependencies.md               ← Phase dependency graph
├── 00-success-criteria-index.md     ← Success criteria per phase
├── DEPENDENCIES.lock.md             ← Pinned ecosystem version and canonical source references
├── CANONICAL_PATHS.md               ← Maps plan concepts to exact ecosystem file/directory locations
├── EXECUTION.md                     ← Deterministic Phase 0B deployment runbook
├── systems.yaml                     ← System inventory
├── deep-scan-v3.md                  ← Deep scan findings archive
│
└── tools/                           ← Validation scripts
    ├── validate_plan_refs.py        ← Plan file cross-reference validation
    ├── validate_skillpacks_registry.py ← Skill pack registry consistency
    └── validate_consistency.py      ← Overall plan consistency checks
```

---

## Navigation Patterns

### "What are we building next?"
1. `Aspire-Production-Roadmap.md` → Current phase + timeline
2. `phases/phase-0b-tower-setup.md` → Detailed execution plan for current phase
3. `EXECUTION.md` → Deterministic Phase 0B deployment runbook (step-by-step)

### "How does layer X work?"
1. `reference/layer-specs/{layer}-spec.md` → Bridge doc with architecture, components, zip paths
2. `reference/ecosystem-asset-index.md` → Find specific files in the ecosystem zip

### "What's the schema for receipts/tokens/risk tiers?"
1. `schemas/` → YAML definitions (single source of truth)
2. `artifacts/` → SQL DDL generated from schemas

### "Is feature X production ready?"
1. `gates/` → Gate definitions and requirements
2. `registries/gate-satisfaction.yaml` → Gate status per phase

### "What skill packs exist?"
1. `registries/skill-pack-registry.yaml` → All 15 packs with agents, APIs, risk tiers
2. `skill-packs/` → Individual pack specifications

### "How do layers talk to each other?"
1. `reference/layer-specs/integration-map.md` → Full data flow + worked examples (GREEN/YELLOW/RED)

---

## Conventions

### Schema Authority
- **Schemas in `schemas/` are canonical.** No inline schema definitions elsewhere.
- SQL in `artifacts/` is generated from schemas, not the other way around.
- If a schema conflict exists, `schemas/` wins.

### Phase Files
- Each phase file has: objectives, tasks (numbered), acceptance criteria, transition gates.
- Tasks use format: `0B.01`, `1.01`, etc. (phase.task_number).
- Status values: `NOT STARTED`, `IN PROGRESS`, `COMPLETE`.

### Layer Specs
- Layer specs are **bridge documents** — they explain what exists in the ecosystem zip, where to find it, and which phase builds it.
- They reference zip paths like `platform/brain/router/router_policy.yaml`.
- They include an "Implementation Readiness" percentage and "V1 Recommendation" section.

### Registries
- YAML format, machine-readable.
- Cross-reference other plan docs by relative path.
- `conflict-registry.yaml` tracks all naming/schema conflicts with resolutions.

### Gates
- 11 gates (00-10) across 5 categories (Testing, Observability, Reliability, Operations, Security).
- Gate satisfaction is tracked in `registries/gate-satisfaction.yaml`.
- **ALL gates must pass before production.** No shortcuts.

### Risk Tiers
- Always use `green / yellow / red` (lowercase).
- Never use `low / medium / high` (legacy naming, resolved in Phase 0).
- Canonical definition: `schemas/risk-tiers.enum.yaml`.

### Naming
- `suite_id` is the primary tenant identifier (not `tenant_id`).
- `office_id` scopes within a suite.
- Canonical definition: `schemas/tenant-identity.yaml`.

---

## Relationship to Other Key Files

| File | Relationship |
|------|-------------|
| `CLAUDE.md` (project root) | Governance authority — plan folder implements what CLAUDE.md mandates |
| `platform/` (ecosystem zip) | Source material — layer specs bridge plan folder to ecosystem content |
| `.claude/commands/` | Session tooling — `/new-session` and `/check-session` reference plan status |
| `plan/reference/agent-onboarding.md` | Agent-focused — architecture + governance quick start |
| `plan/ONBOARDING.md` | Plan-folder-focused — structure + navigation + conventions (this file) |

---

## What's NOT in the Plan Folder

- **Implementation code** — Lives in `backend/`, `mobile/`, `infrastructure/`, `tests/`
- **Ecosystem zip raw files** — Referenced via layer specs, not copied
- **Agent output logs** — Live in `docs/agents/`
- **Session state** — Live in `.claude/` and Knowledge Graph
- **Secrets/credentials** — Never in plan folder, ever

---

## Maintaining the Plan Folder

### When adding a new phase
1. Create `phases/phase-X-name.md` with standard structure
2. Add to `Aspire-Production-Roadmap.md` phase table
3. Add to `README.md` navigation
4. Update `registries/phase-mapping.yaml` if MANIFEST mapping needed
5. Update `registries/gate-satisfaction.yaml` with affected gates

### When adding a new schema
1. Create `schemas/name.schema.v1.yaml`
2. Generate SQL artifact in `artifacts/`
3. Add to `README.md` schemas table
4. Reference in relevant phase file

### When updating layer specs
1. Edit the relevant file in `reference/layer-specs/`
2. Update readiness percentage if implementation progressed
3. Update `Aspire-Production-Roadmap.md` readiness scores table

---

**End of Plan Folder Onboarding**
