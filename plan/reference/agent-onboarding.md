# Aspire Agent Onboarding Guide

**Purpose:** Get any Claude Code agent (main or subagent) productive in <2 minutes.
**Last Updated:** 2026-02-07 | **Current Phase:** 0B (Trust Spine Deploy)

---

## What Is Aspire?

Aspire is a **governed AI execution platform** for small business professionals. AI labor safely touches reality through an immutable audit trail (receipts), capability-scoped tokens, and risk-tiered approval flows.

**It is NOT a chatbot.** Every action follows:
```
Intent → Context → Plan → Policy Check → Approval → Execute → Receipt → Summary
```

---

## The 7 Immutable Laws (Non-Negotiable)

| # | Law | One-Liner |
|---|-----|-----------|
| 1 | **Single Brain** | Only LangGraph orchestrator decides. Tools propose, orchestrator disposes. |
| 2 | **Receipt for All** | Every state change → immutable, append-only receipt. No exceptions. |
| 3 | **Fail Closed** | Missing permission/policy/verification = deny. Never guess. |
| 4 | **Risk Tiers** | GREEN (auto) / YELLOW (user confirm) / RED (explicit authority + strong UX). |
| 5 | **Capability Tokens** | Short-lived (<60s), scoped (tenant + tool + action), server-verified. |
| 6 | **Tenant Isolation** | RLS at DB layer. Zero cross-tenant leakage. Ever. |
| 7 | **Tools Are Hands** | MCP tools execute bounded commands. They never decide or retry autonomously. |

**Full governance:** `CLAUDE.md` (project root)

---

## Architecture: 3-Layer Stack

```
┌─────────────────────────────────────────────────────┐
│  INTELLIGENCE LAYER (Brain)                          │
│  LangGraph orchestrator, LLM router, QA loop,       │
│  agent personas, state machines                      │
│  Spec: plan/reference/layer-specs/brain-spec.md      │
├─────────────────────────────────────────────────────┤
│  TRUST SPINE (Gateway + Trust)                       │
│  Policy enforcement, capability tokens, receipts,    │
│  approvals, outbox, A2A messaging, RLS              │
│  Specs: gateway-spec.md, trust-spine-spec.md         │
├─────────────────────────────────────────────────────┤
│  STATE LAYER (Supabase)                              │
│  Postgres + Auth + Realtime + Edge Functions         │
│  Redis/Upstash queues, S3 blobs                      │
└─────────────────────────────────────────────────────┘
```

**Data flow:** Intent → Brain → Gateway → Trust Spine → Supabase
**Cross-layer rules:** See `plan/reference/layer-specs/integration-map.md`

---

## File Navigation (Where Things Live)

### Governance & Rules
| File | Content |
|------|---------|
| `CLAUDE.md` | 7 Immutable Laws, production gates, agent rules |
| `Rules.md` | Supplementary rules |
| `.claude/settings.json` | Project settings |

### Plan Folder (Roadmap + Specs)
| Path | Content |
|------|---------|
| `plan/README.md` | Navigation index (start here) |
| `plan/Aspire-Production-Roadmap.md` | Lean roadmap v5.0 (<200 lines) |
| `plan/phases/` | Phase files (0 through 6 + Compliance) |
| `plan/schemas/` | Canonical schemas (receipts, tokens, risk tiers, tenant ID) |
| `plan/registries/` | Skill packs, phase mapping, gates, conflicts |
| `plan/gates/` | 11 production gates (00-10) |
| `plan/artifacts/` | SQL schema artifacts |

### Layer Specifications (Ecosystem ZIP Bridge Docs)
| Spec | Readiness | Phase |
|------|-----------|-------|
| [trust-spine-spec.md](layer-specs/trust-spine-spec.md) | 70% | 0B |
| [brain-spec.md](layer-specs/brain-spec.md) | 10% | 1 |
| [gateway-spec.md](layer-specs/gateway-spec.md) | 15% | 1 |
| [control-plane-spec.md](layer-specs/control-plane-spec.md) | 40% | 2 |
| [supporting-layers.md](layer-specs/supporting-layers.md) | Varies | 2+ |
| [integration-map.md](layer-specs/integration-map.md) | Reference | All |

### Ecosystem ZIP (Pre-Built Assets)
| Path | Content |
|------|---------|
| `plan/reference/ecosystem-asset-index.md` | Full inventory of 2,039 files |
| `platform/trust-spine/` | 872 files: migrations, edge functions, Go verifier |
| `platform/brain/` | 52 files: personas, state machines, QA loop, router |
| `platform/gateway/` | 59 files: policies, tools catalog, safety guards |
| `platform/control-plane/` | 75 files: registry, rollouts, certification |

---

## Current Phase Status

**Phase 0:** COMPLETE (Foundation Sync — schemas, registries, roadmap, conflicts resolved)
**Phase 0A:** COMPLETE (19 cloud accounts, API keys, repository)
**Phase 0B:** NOT STARTED (Trust Spine deploy — apply Trust Spine canonical migrations per MIGRATION_ORDER_ADDON.md, see CANONICAL_PATHS.md for exact paths and counts. Deploy 5 core Edge Functions + optional A2A addon, 7 migrations + 3 Edge Functions, Go service)
**Phase 1:** NOT STARTED (LangGraph orchestrator — Brain + Gateway, 7 weeks)

**Next action:** Deploy Trust Spine substrate per `plan/phases/phase-0b-tower-setup.md`
**Transition gate 0B->1:** Trust Spine canonical migrations deployed (see CANONICAL_PATHS.md), RLS verified, Edge Functions live

---

## Key Schemas (Single Source of Truth)

All systems reference these. No inline definitions elsewhere.

| Schema | Canonical File |
|--------|---------------|
| Risk Tiers | `plan/schemas/risk-tiers.enum.yaml` → `green / yellow / red` |
| Receipts | `plan/schemas/receipts.schema.v1.yaml` → 15+ mandatory fields |
| Capability Tokens | `plan/schemas/capability-token.schema.v1.yaml` → <60s, HMAC-SHA256 |
| Tenant Identity | `plan/schemas/tenant-identity.yaml` → `suite_id` is canonical |
| Approval Status | `plan/schemas/approval-status.enum.yaml` |
| Outcome Status | `plan/schemas/outcome-status.enum.yaml` |

---

## Agent Roster (Brain Layer)

11 agents, all governed by 7 Laws through shared foundation blocks:

| Agent | Role | Domain | Risk |
|-------|------|--------|------|
| **Ava** | Orchestrator | routing/governance | — |
| **Sarah** | Front Desk | telephony | LOW-MED |
| **Adam** | Research Desk | web/vendor research | LOW-MED |
| **Finn** | Money Desk | finance (propose only) | HIGH |
| **Milo** | Payroll Desk | payroll (read only, submit=HARD DENY) | HIGH |
| **Teressa** | Books Desk | bookkeeping (read only) | MED |
| **Eli** | Inbox/Email | email ops | MED |
| **Quinn** | Revenue Ops | quotes/invoices | MED |
| **Nora** | Conference Room | meetings (DENIED: email, calendar, money, contracts) | LOW-MED |
| **TEC** | Documents | PDF render, preflight | LOW |
| **Clara** | Legal Desk | contracts | HIGH |

**Visibility:** Finn, Milo, Teressa are `internal_frontend` only.

---

## Production Gates (5 Categories, 11 Gates)

Nothing is "production ready" without passing ALL gates:

| Gate | Category | Phase |
|------|----------|-------|
| Testing | RLS isolation, evil tests, replay demo, >=80% coverage | 1 |
| Observability | SLO dashboard, correlation IDs, health checks | 4 |
| Reliability | Circuit breakers, idempotent retries, timeouts | 1 |
| Operations | Runbooks, soak plan, rollback procedures | 4 |
| Security | 5-pillar review, secrets management, DLP/PII redaction | 1 |

---

## Governance Quick Checks

When building anything, verify against this table:

| Change Type | Check |
|-------------|-------|
| New API endpoint | Receipts? Capability tokens? Risk tier? |
| Database migration | RLS preserved? Receipt immutability (no UPDATE/DELETE)? |
| New MCP tool | Execute-only (no decisions)? Token validation? Returns receipt? |
| State change | Receipt generated? Approval flow for YELLOW/RED? |
| External API call | Capability token? Timeout/circuit breaker? Receipt on success AND failure? |
| Auth/permission | Tenant isolation preserved? RLS updated? Evil tests needed? |

---

## Quick Start Checklist

1. Read this file (you're here)
2. Read `plan/Aspire-Production-Roadmap.md` for current phase
3. Read the relevant phase file from `plan/phases/`
4. Read the relevant layer spec from `plan/reference/layer-specs/`
5. Read `plan/reference/layer-specs/integration-map.md` for cross-layer flow
6. Reference `plan/schemas/` for data structure questions
7. Check `plan/registries/` for cross-reference lookups
8. Check `plan/gates/` for production readiness requirements
9. Search `plan/reference/ecosystem-asset-index.md` for pre-built assets

**Governance:** All code must comply with `CLAUDE.md` (7 Immutable Laws).

---

**End of Agent Onboarding Guide**
