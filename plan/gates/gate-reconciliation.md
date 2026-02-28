# Gate System Reconciliation

**Purpose:** Map the 11 Roadmap Gate Checkpoints to the 5 CLAUDE.md Production Gate Categories
**Created:** 2026-02-07 | **Resolves:** Deep Scan H3

---

## Two Complementary Systems

Aspire has two gate systems that are **complementary, not conflicting**:

1. **CLAUDE.md Production Gate Categories** (5 domains that must pass)
2. **Roadmap Gate Checkpoints** (11 specific artifacts/tests that must exist)

The categories define **what domains** need verification. The checkpoints define **what specific things** are verified. Every checkpoint maps to one or more categories.

---

## CLAUDE.md: 5 Production Gate Categories

| # | Category | What It Covers |
|---|----------|---------------|
| Cat 1 | **Testing** | RLS isolation, evil tests, replay demo, code coverage >= 80% |
| Cat 2 | **Observability** | SLO dashboards, correlation IDs, health checks |
| Cat 3 | **Reliability** | Circuit breakers, retries, backoff, timeouts |
| Cat 4 | **Operations** | Runbooks, postmortem template, rollback, soak plan |
| Cat 5 | **Security** | 5-pillar review, secrets management, DLP/PII |

---

## Roadmap: 11 Gate Checkpoints mapped to Categories

| Gate | Name | CLAUDE.md Category | Phase Introduced | Type |
|------|------|-------------------|------------------|------|
| 00 | Scope Lock | *Product* (not production) | 0A | Product |
| 01 | UI Surface Invariants | *Product* (not production) | 3 | Product |
| 02 | Call State Machine | *Product* (not production) | 3 | Product |
| 03 | Forced Escalation | Cat 5: Security | 1 | Production |
| 04 | Degradation Ladder | Cat 3: Reliability | 3 | Production |
| 05 | Cold/Warm/Hot States | Cat 4: Operations | 3 | Production |
| 06 | Receipts Immutable | Cat 1: Testing | 1 | Production |
| 07 | RLS Isolation | Cat 1: Testing + Cat 5: Security | 1 | Production |
| 08 | Replay Demo | Cat 1: Testing | 4 | Production |
| 09 | SLO Dashboard | Cat 2: Observability | 4 | Production |
| 10 | Incident Runbooks | Cat 4: Operations | 4 | Production |

### Key Insight: Product Gates vs Production Gates

Gates 00-02 are **Product Gates** (define what the product must look like). They don't map directly to CLAUDE.md's production gate categories because CLAUDE.md focuses on operational readiness, not product design.

Gates 03-10 are **Production Gates** that map cleanly to CLAUDE.md categories.

---

## Category Coverage Analysis

### Cat 1: Testing
- Gate 06: Receipts Immutable (append-only, hash-chain)
- Gate 07: RLS Isolation (zero cross-tenant leakage)
- Gate 08: Replay Demo (deterministic reconstruction)
- **Additional CLAUDE.md requirements:** Evil tests, code coverage >= 80%

### Cat 2: Observability
- Gate 09: SLO Dashboard (p50/p95/p99, error budgets)
- **Additional CLAUDE.md requirements:** Correlation IDs flowing, health checks

### Cat 3: Reliability
- Gate 04: Degradation Ladder (Video -> Audio -> Async -> Text)
- **Additional CLAUDE.md requirements:** Circuit breakers, retries with idempotency, backoff, timeouts

### Cat 4: Operations
- Gate 05: Cold/Warm/Hot States (interaction mode management)
- Gate 10: Incident Runbooks (outage procedures, game-day)
- **Additional CLAUDE.md requirements:** Postmortem template, rollback procedures, soak plan

### Cat 5: Security
- Gate 03: Forced Escalation (video for RED tier)
- Gate 07: RLS Isolation (tenant separation)
- **Additional CLAUDE.md requirements:** 5-pillar review, secrets management, DLP/PII

---

## Phase Timeline

```
Phase 0A:  Gate 00 (Scope Lock) .................. Product
Phase 1:   Gate 03, 06, 07 ...................... Security, Testing
Phase 3:   Gate 01, 02, 04, 05 .................. Product, Reliability, Operations
Phase 4:   Gate 08, 09, 10 ...................... Testing, Observability, Operations
           + ALL 5 CLAUDE.md categories verified
```

---

## Enforcement Rules

1. **Both systems must pass.** All 11 roadmap gates AND all 5 CLAUDE.md categories.
2. **Category gaps.** Some CLAUDE.md requirements (evil tests, circuit breakers, correlation IDs) don't have dedicated gate checkpoints. These are verified during Phase 4 as part of the 10/10 Bundle.
3. **Gate regression.** Once satisfied, a gate cannot regress. Any regression triggers incident protocol.
4. **Product gates are separate.** Gates 00-02 are product design gates, not operational readiness gates. They're still mandatory for launch.

---

**End of Gate Reconciliation**
