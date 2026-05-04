---
name: Trust Hub PRR Patterns
description: Key findings from the per-tenant Trust Hub + CNAM full PRR (2026-05-04) — receipt taxonomy, metrics gap, test file naming, risk tier precedents
type: project
---

## Gate 2 (Observability) Gap — Trust Hub Metrics Not in metrics.py

**Fact:** When the trust_onboarding worker was built, 7 required Prometheus metrics (Counter/Histogram/Gauge) were not added to `services/metrics.py`. The plan specified them in §12 Gate 2 but implementation omitted them.

**Why:** Worker code was written by `mcp-toolsmith` which focused on business logic; metrics instrumentation was deferred.

**How to apply:** For any future PRR on a new worker or long-running ARQ job, check `services/metrics.py` for new metric registrations. If none exist for the new feature, mark Gate 2 CONDITIONAL PASS and add as P0 post-ship follow-up.

---

## Receipt Type Docstring Drift

**Fact:** `trust_receipts.py` header docstring says "21 trust-related receipt types" but the actual `RECEIPT_TYPES` frozenset has 35. Docstrings drifted during implementation as waves added types.

**How to apply:** When verifying receipt coverage in DoD Section 6, always count the frozenset directly (`len(RECEIPT_TYPES)`) rather than trusting the docstring comment. Flag docstring-count mismatches as follow-up items.

---

## Risk Tier Precedent — Trust Hub

All 35 trust receipt types are YELLOW tier (state-changing, PII-touching, external API side effects). No Red-tier operations in this feature. Established precedent: Twilio Trust Hub operations are Yellow by default, even though they involve PII, because they are reversible and do not involve financial transactions.

---

## Test File Naming Pattern — Trust Hub

Confirmed test files for trust_onboarding features follow this pattern:
- `test_trust_*.py` — state machine, routes, RLS, receipts, E2E
- `test_cnam_*.py` — sanitizer, display-name change
- `test_a2p_*.py` — A2P 10DLC registration flows
- `test_swap_*.py` — W11 number swap
- `test_backfill_*.py` — W10 existing-tenant backfill
- `test_admin_trust_*.py` — admin batch backfill routes
- `test_cron_jobs.py` — W9 reputation polling
- `test_branded_calling.py` — W6 feature-flag gating

**Missing from plan but not created:** `test_trust_evil.py` — plan mandated it for cross-tenant injection and PII scan, but it was never created. The PII guardrails are in `test_trust_receipts.py` instead; cross-tenant DB-trigger tests are in `test_trust_rls_isolation.py`. This split is acceptable but creates a Gate 1 gap vs the plan spec.

---

## Pre-Deploy Blocker Pattern — Redis URL in Railway

The most dangerous silent failure mode in this feature: if `ASPIRE_REDIS_URL` is not set in Railway, `POST /v1/trust-hub/kyb` returns HTTP 200 and stores KYB data in Supabase, but `_enqueue_advance_trust_state()` fails with a warning log and returns False (swallowed error). The tenant is silently stuck at `kyb_collected` forever with no error indication. Always check Redis URL configuration before deploying any ARQ worker feature.

---

## Soak Test Gap Pattern

Gate 4 requires 24h staging soak before W2 worker ships to production. The per-tenant trust hub plan specifies "1 tenant/min synthetic onboarding loop." No soak test file was created during implementation. This is a recurring pattern — soak tests are planned but not written during wave development. Flag this immediately in any PRR Gate 4 review for new worker features.

---

## Migration Count (Trust Hub)

Migrations 109–120 = 12 migrations for the full trust hub + CNAM feature. Migration 113 was an unplanned security hardening patch added after security-reviewer found THREAT-001/003 in W1 schema. Plan originally specified 109–113 but grew to 120 through waves. Always verify actual migration file count vs plan spec.

---

## W7 A2P Capability Token Audit Gap

`a2p_state_machine.py` does not forward `capability_token_id` to `cut_trust_receipt()` calls. This creates a Law #5 audit gap — A2P state transitions cannot be traced back to the capability token that authorized them. Flag this pattern whenever reviewing A2P-related receipt writes in future PRRs.
