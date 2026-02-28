# Roadmap Sync (Ava Admin -> Aspire Phases)

**Source:** Ava Admin Enterprise Handoff v2

## Phase alignment

| Plan.zip Phase | Aspire Phase | Name |
|---|---|---|
| phase-0a-laptop-prep.md | 0A | Laptop-Compatible Prep |
| phase-0b-tower-setup.md | 0B | Trust Spine Deployment + Skytech Tower Setup |
| phase-1-orchestrator.md | 1 | Capability Tokens + Receipt Ledger (1A Substrate + 1B Intelligence) |
| phase-2-founder-mvp.md | 2 | Founder Quarter MVP - 4 Skill Packs |

## Recommended placement
- **Phase 1** (Core Orchestrator + Safety): build receipts/capability token enforcement + telemetry facade endpoints
- **Phase 2** (Founder MVP): ship Admin Portal pages wired to telemetry + Admin Ava panel in read-only mode
- **Phase 3+** (Certification): enforce ChangeProposal -> evals/robots -> approvals -> rollouts

Admin Ava should start as **read-only + explain** before any execute permissions exist.

## Acceptance criteria (Enterprise Grade)

### Safety / governance
- 100% privileged actions require: capability token + approval receipt + outbox job + execution receipt
- 0 shadow execution paths from Admin UI
- Tenant isolation: no cross-suite/office reads (RLS tests pass)

### Observability
- Admin Ava can produce evidence-backed summaries using: receipts + provider call logs + traces + rollout state
- Golden signals dashboard exists

### Change management
- Registry items are versioned; state machine enforced (draft->staged->active...)
- Canary rollout supported with rollback triggers
- Promotions blocked unless eval/robots receipts exist

### No-code usability
- Operator Mode: where-to-click + what it means + safest next step
- Engineer Mode: raw IDs/diffs/policy keys without changing underlying truth
