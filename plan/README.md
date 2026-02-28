# Aspire Plan - Navigation Index

**Last Updated:** 2026-02-07 | **Current Phase:** Phase 0B Trust Spine Deploy (NEXT)

---

## Onboarding

| Guide | Audience | Content |
|-------|----------|---------|
| **[ONBOARDING.md](ONBOARDING.md)** | Anyone navigating the plan folder | Structure, conventions, navigation patterns |
| **[Agent Onboarding](reference/agent-onboarding.md)** | New Claude Code agents | Architecture, governance, file locations, quick start |

---

## Roadmap

**[Aspire-Production-Roadmap.md](Aspire-Production-Roadmap.md)** - Lean executive roadmap (v5.0, <200 lines)
- Timeline, phase summaries, gate checklist, schema status, API wiring

---

## Phases

| Phase | File | Status |
|-------|------|--------|
| **0: Foundation Sync** | [phase-0-foundation-sync.md](phases/phase-0-foundation-sync.md) | **COMPLETE** |
| 0A: Cloud Accounts | [phase-0a-laptop-prep.md](phases/phase-0a-laptop-prep.md) | COMPLETE |
| 0B: Trust Spine Deploy | [phase-0b-tower-setup.md](phases/phase-0b-tower-setup.md) | Not Started |
| 1: Core Orchestrator | [phase-1-orchestrator.md](phases/phase-1-orchestrator.md) | Not Started |
| 2: Skill Packs | [phase-2-founder-mvp.md](phases/phase-2-founder-mvp.md) | Not Started |
| 3: Mobile App | [phase-3-mobile-app.md](phases/phase-3-mobile-app.md) | Not Started |
| 4: Hardening | [phase-4-hardening.md](phases/phase-4-hardening.md) | Not Started |
| 5: Beta Launch | [phase-5-beta-launch.md](phases/phase-5-beta-launch.md) | Not Started |
| 6: Scale & Expand | [phase-6-scale-expand.md](phases/phase-6-scale-expand.md) | Not Started |
| C: Compliance | [phase-compliance-parallel.md](phases/phase-compliance-parallel.md) | Parallel |

---

## Canonical Schemas (Single Source of Truth)

All systems MUST reference these schemas. No inline definitions elsewhere.

| Schema | File | Governs |
|--------|------|---------|
| Risk Tiers | [risk-tiers.enum.yaml](schemas/risk-tiers.enum.yaml) | green/yellow/red (Law #4) |
| Receipts | [receipts.schema.v1.yaml](schemas/receipts.schema.v1.yaml) | 15+ fields, SQL + TS + JSON (Law #2) |
| Capability Tokens | [capability-token.schema.v1.yaml](schemas/capability-token.schema.v1.yaml) | <60s, HMAC-SHA256 (Law #5) |
| Tenant Identity | [tenant-identity.yaml](schemas/tenant-identity.yaml) | suite_id primary (Law #6) |
| Approval Status | [approval-status.enum.yaml](schemas/approval-status.enum.yaml) | pending/approved/rejected/expired/canceled |
| Outcome Status | [outcome-status.enum.yaml](schemas/outcome-status.enum.yaml) | success/denied/failed/timeout/pending |

---

## Registries

| Registry | File | Tracks |
|----------|------|--------|
| Skill Packs | [skill-pack-registry.yaml](registries/skill-pack-registry.yaml) | 15 packs: character, APIs, risk tiers, certification |
| Phase Mapping | [phase-mapping.yaml](registries/phase-mapping.yaml) | MANIFEST A-F to Roadmap 0A-6 |
| Gate Satisfaction | [gate-satisfaction.yaml](registries/gate-satisfaction.yaml) | 11 gates: status, evidence, phase |
| Conflict Registry | [conflict-registry.yaml](registries/conflict-registry.yaml) | 20 deep scan conflicts with resolutions |

---

## Gates

| Resource | File |
|----------|------|
| Gates Overview | [gates/README.md](gates/README.md) |
| Gate Reconciliation | [gates/gate-reconciliation.md](gates/gate-reconciliation.md) |
| Individual Gates | [gate-00](gates/gate-00-scope-lock.md) through [gate-10](gates/gate-10-incident-runbooks.md) |

---

## SQL Artifacts

| Artifact | File |
|----------|------|
| Receipts Schema | [artifacts/receipts-schema.sql](artifacts/receipts-schema.sql) |
| Capability Tokens | [artifacts/capability-tokens-schema.sql](artifacts/capability-tokens-schema.sql) |
| Checkpoints | [artifacts/checkpoints-schema.sql](artifacts/checkpoints-schema.sql) |
| Identity + RLS | [artifacts/identity-rls-schema.sql](artifacts/identity-rls-schema.sql) |

---

## Platform Layer Specifications (Ecosystem ZIP Bridge Docs)

Enterprise-grade specifications bridging the plan folder to the 2,039-file ecosystem zip (v12.7).

| Spec | File | Coverage |
|------|------|----------|
| **Trust Spine** | [layer-specs/trust-spine-spec.md](reference/layer-specs/trust-spine-spec.md) | 872 files: migrations, edge functions, Go verifier, outbox, A2A, policy engine |
| **Brain** | [layer-specs/brain-spec.md](reference/layer-specs/brain-spec.md) | 52 files: agent registry, state machines, LLM router, QA loop, eval harness |
| **Gateway** | [layer-specs/gateway-spec.md](reference/layer-specs/gateway-spec.md) | 59 files: policy wiring, 35 tools, safety guards, PII redaction, provider integrations |
| **Control Plane** | [layer-specs/control-plane-spec.md](reference/layer-specs/control-plane-spec.md) | 75 files: registry lifecycle, canary rollout, certification, runbooks |
| **Supporting Layers** | [layer-specs/supporting-layers.md](reference/layer-specs/supporting-layers.md) | n8n, observability, providers, services, finance office, contracts, +7 more |
| **Integration Map** | [layer-specs/integration-map.md](reference/layer-specs/integration-map.md) | Cross-layer data flow, error propagation, receipt threading, token lifecycle |
| **Ecosystem Index** | [reference/ecosystem-asset-index.md](reference/ecosystem-asset-index.md) | Complete inventory of all 2,039 files mapped to plan docs + phases |

---

## Reference Documents (Extracted from Roadmap v4.2)

| Document | File | Content |
|----------|------|---------|
| Changelog | [reference/changelog.md](reference/changelog.md) | Roadmap version history v1.1-v4.2 |
| Worth Assessment | [reference/worth-assessment.md](reference/worth-assessment.md) | Pre-implementation validation (88% confidence) |
| Technology Stack | [reference/technology-stack.md](reference/technology-stack.md) | Locked tech decisions |
| Ecosystem Architecture | [reference/ecosystem-architecture.md](reference/ecosystem-architecture.md) | Platform layers, contracts, evolution doctrine |
| Triple-Memory Strategy | [reference/triple-memory-strategy.md](reference/triple-memory-strategy.md) | KG + Serena + Session Reflection |

---

## Supporting Files

| Resource | File |
|----------|------|
| Phase Dependencies | [00-dependencies.md](00-dependencies.md) |
| Success Criteria | [00-success-criteria-index.md](00-success-criteria-index.md) |
| Dependencies Lock | [DEPENDENCIES.lock.md](DEPENDENCIES.lock.md) | Pinned ecosystem version and canonical source references |
| Canonical Paths | [CANONICAL_PATHS.md](CANONICAL_PATHS.md) | Maps plan concepts to exact ecosystem file/directory locations |
| Execution Runbook | [EXECUTION.md](EXECUTION.md) | Deterministic Phase 0B deployment runbook |
| Skill Pack Specs | [skill-packs/](skill-packs/) |
| Business Plan | [Aspire_Business_Plan.md](Aspire_Business_Plan.md) |
| Trust Spine Docs | [trust-spine-spec.md](reference/layer-specs/trust-spine-spec.md) |
| Systems YAML | [systems.yaml](systems.yaml) |
| Deep Scan v3 | [deep-scan-v3.md](deep-scan-v3.md) |

---

## Validation Tools

| Tool | File | Purpose |
|------|------|---------|
| Plan Ref Validator | [tools/validate_plan_refs.py](tools/validate_plan_refs.py) | Validates plan file cross-references |
| Skill Pack Validator | [tools/validate_skillpacks_registry.py](tools/validate_skillpacks_registry.py) | Validates skill pack registry consistency |
| Consistency Checker | [tools/validate_consistency.py](tools/validate_consistency.py) | Validates overall plan consistency |

---

## For Agents: Quick Start

1. Read this file for navigation
2. Read [Aspire-Production-Roadmap.md](Aspire-Production-Roadmap.md) for current status
3. Read the relevant phase file from [phases/](phases/)
4. Read the relevant layer spec from [reference/layer-specs/](reference/layer-specs/) for implementation details
5. Read [integration-map.md](reference/layer-specs/integration-map.md) for cross-layer data flow
6. Reference [schemas/](schemas/) for any data structure questions
7. Check [registries/](registries/) for cross-reference lookups
8. Check [gates/](gates/) for production readiness requirements
9. Search [ecosystem-asset-index.md](reference/ecosystem-asset-index.md) to find pre-built assets in the ecosystem zip

**Governance:** All code must comply with CLAUDE.md (7 Immutable Laws). See `CLAUDE.md` at project root.

---

**End of Navigation Index**
