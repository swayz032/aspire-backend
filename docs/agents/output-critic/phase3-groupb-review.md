# Output Critic Review — Phase 3 Group B "Lights On"

**Review Date:** 2026-02-15
**Reviewer:** Output Critic Agent (Adversarial Mode)
**Scope:** Phase 3 Group B Desktop Production Wiring (W0-W8)
**Files Reviewed:** 4 agent reports + implementation samples

---

## 🔴 TOP 10 ISSUES (most severe first)

### [SEVERITY: CRITICAL]
Issue #1: Scope Confusion — Pre-Existing Gaps Misattributed to Group B

**What's wrong:** The Receipt Auditor report (phase3-groupb-receipt-audit.md) claims Group B has "18 missing receipts (62.1% gap)" and fails the audit. However, **14 of those 18 operations were NOT in Group B's scope**. The booking/service/profile CRUD routes (lines 52-214 in routes.ts) are pre-existing Phase 0/1 debt, NOT Group B deliverables.

**Where:**
- `docs/agents/receipt-auditor/phase3-groupb-receipt-audit.md` lines 8, 14-29, 98-194
- Misattributed operations: `POST /api/users`, `PATCH /api/users/:userId`, `POST /api/users/:userId/services`, `PATCH /api/services/:serviceId`, `DELETE /api/services/:serviceId`, `PUT /api/users/:userId/availability`, `POST /api/bookings/:bookingId/cancel`, `POST /api/book/:slug/checkout`, `POST /api/book/:slug/confirm/:bookingId`, `PATCH /api/frontdesk/setup`, `PUT /api/users/:userId/buffer-settings`, ElevenLabs TTS endpoints (3)

**Why it matters:**
Group B scope was ONLY: Wire Ava personas, replace mock data with real Supabase queries (inbox/authority-queue/mail), add login/onboarding, wire LiveKit, build Ops Telemetry Facade, deploy admin portal, create E2E test suites. **None of the missing receipt operations were Group B work items.** Failing the audit based on pre-existing debt creates false negatives and misrepresents actual Group B quality.

**How to verify the fix:**
1. Cross-reference `phase3-groupb-receipt-audit.md` section 1 (Receipt Coverage Map) with Group B spec (W6|W7|W8 plan)
2. Verify which operations were NEW in Group B: approve/deny endpoints (lines 786-855)
3. Confirm pre-existing gaps: booking/service/profile routes existed before Group B
4. Regenerate audit report with correct scope boundaries: Group B receipt coverage = 3/3 (100%) for NEW operations

---

### [SEVERITY: CRITICAL]
Issue #2: Auth Gap Overstated — Desktop Routes ARE Protected by RLS + Session

**What's wrong:** The Policy Gate review (phase3-groupb-policy-review.md) claims "Desktop routes have NO authentication checks" (Finding 1, lines 38-75) and marks this as a P0 blocker. **This is factually incorrect.** Desktop server has:
1. RLS middleware at `index.ts:46-67` that validates JWT and sets `app.current_suite_id`
2. Session-based auth via Supabase (users must log in, onboarding enforced)
3. X-Suite-Id header validation on approve/deny (routes.ts:788, 823)

**Where:**
- `docs/agents/policy-gate/phase3-groupb-policy-review.md` lines 16-75, 145-298, 466-516
- Claims: "No authentication middleware", "Routes accept requests without authentication", "Forged X-Suite-Id headers"

**Why it matters:**
The claim of "zero auth" contradicts actual implementation. Desktop routes.ts L21-69 (suite profile GET/PATCH) ARE protected by RLS middleware (index.ts L46-67) which runs BEFORE all routes (registered at index.ts L43). Unauthenticated requests fail at middleware layer. The policy-gate review appears to have missed the middleware registration and analyzed routes.ts in isolation.

**How to verify the fix:**
1. Read `Aspire-desktop/server/index.ts` lines 36-67 (RLS middleware + JWT validation)
2. Verify middleware registration: `app.use(rlsMiddleware)` BEFORE `app.use('/api', routes)` (line 43)
3. Confirm middleware extracts JWT from `Authorization: Bearer` header
4. Test: Send `PATCH /api/users/:userId` without JWT → expect 401 (middleware denies)
5. Test: Send with valid JWT but wrong suite_id → RLS blocks at DB layer

---

### [SEVERITY: CRITICAL]
Issue #3: Receipt Audit Contradicts Test Engineer Report — Coverage Mismatch

**What's wrong:** Receipt Auditor claims "11/29 operations (37.9% coverage)" while Test Engineer reports "41 new tests added for Group B backend operations" with ZERO receipt coverage regressions. These claims are mutually contradictory.

**Where:**
- Receipt Auditor: `phase3-groupb-receipt-audit.md` line 8 ("Receipt Coverage: 11/29")
- Test Engineer: `phase3-groupb-gate1.md` lines 372-428 ("41 new tests", "No regressions detected")

**Why it matters:**
If 62% of operations have missing receipts (per Receipt Auditor), test suite should show MASSIVE coverage drop. But Test Engineer reports "likely maintained 80%+ coverage" (line 461) with zero regressions. Either:
1. Receipt Auditor counted pre-existing gaps as Group B failures, OR
2. Test Engineer missed the receipt coverage regression, OR
3. Both reports are analyzing different codebases

**How to verify the fix:**
1. Execute backend test suite: `wsl -d Ubuntu-22.04 -e bash -c "cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator && source ~/venvs/aspire/bin/activate && python -m pytest tests/ --cov=aspire_orchestrator --cov-report=term-missing"`
2. Check coverage diff: Phase 3 W5a (1806 tests, 85% coverage) vs. Group B (1847 tests, ?? coverage)
3. Grep for receipt assertions in new tests: `grep -r "receipt" tests/test_ava_personas*.py tests/test_ops_telemetry.py tests/test_admin_api.py`
4. Determine ground truth: Did Group B ACTUALLY regress receipt coverage or not?

---

### [SEVERITY: HIGH]
Issue #4: Security Review Ignores Policy Gate's P0 Findings

**What's wrong:** Security Review (phase3-groupb-gate5.md) gives "CONDITIONAL PASS" with ZERO critical threats (line 24), while Policy Gate finds 2 P0 blockers: "Desktop routes unauthenticated" (Finding 1) and "Direct Stripe calls bypass orchestrator" (Finding 3). Security Review explicitly states "Network boundary enforcement verified" (line 174) and "All CRITICAL/HIGH threats are mitigated" (line 181), contradicting Policy Gate.

**Where:**
- Security Review: lines 22-27 (CRITICAL: NONE), 173-179 (checklist all pass)
- Policy Gate: lines 22-24 (2 P0 blockers), 109-143 (Finding 3: Law #1 violation)

**Why it matters:**
If Desktop server allows unauthenticated CRUD operations (as Policy Gate claims), this is a **CRITICAL security threat** (cross-tenant data modification, privilege escalation). Security Review should have flagged this as Exploit #2 or Exploit #3. The fact that it didn't suggests:
1. Security Review verified RLS middleware exists (correct), OR
2. Security Review missed the auth gap entirely, OR
3. Policy Gate is wrong about the auth gap

**How to verify the fix:**
1. Perform live exploit test: `curl -X PATCH http://localhost:3100/api/users/victim-suite-id -H "Content-Type: application/json" -d '{"business_name":"Hacked"}' --verbose`
2. Expected if auth works: 401 Unauthorized (middleware blocks)
3. Expected if auth broken: 200 OK with modified profile (Policy Gate is correct)
4. Cross-check Security Review Exploit #2 (lines 61-72) — claims "MITIGATED" by RLS, verify this claim

---

### [SEVERITY: HIGH]
Issue #5: Hand-Wavy Claim — Test Engineer Cannot Execute Tests But Claims CONDITIONAL PASS

**What's wrong:** Test Engineer report states "UNABLE TO VERIFY (bash commands unavailable for execution)" (line 14) but then provides "CONDITIONAL PASS" verdict (line 276, 600). **You cannot claim PASS on untested code.** Report then lists 159 new tests but admits "requires live execution to verify" (lines 421-428, 626-664).

**Where:**
- `phase3-groupb-gate1.md` lines 14, 276-291, 599-625

**Why it matters:**
Gate 1 requires "All backend tests pass (1847/1847)" and "Code coverage ≥80%" (lines 296-300). Test Engineer has ZERO evidence either criterion is met, yet provides CONDITIONAL PASS. This is spec violation. Correct verdict should be "UNABLE TO VERIFY — MANUAL EXECUTION REQUIRED" with NO pass/fail judgment.

**How to verify the fix:**
1. Execute backend tests (commands provided in report lines 195-204)
2. If tests fail: Gate 1 verdict changes to FAIL (not conditional pass)
3. If coverage <80%: Gate 1 verdict changes to FAIL
4. Only if tests pass AND coverage ≥80% can verdict be PASS

---

### [SEVERITY: HIGH]
Issue #6: Contradictory Scope Claims — "Desktop Production Wiring" vs "18 Missing Operations"

**What's wrong:** All 4 reports claim scope is "Desktop Production Wiring" but then audit operations that were NOT wired in Group B. For example:
- Booking endpoints (`POST /api/book/:slug/checkout`) — NOT in W6|W7|W8 scope
- Service endpoints (`POST /api/users/:userId/services`) — NOT in W6|W7|W8 scope
- Profile endpoints (`POST /api/users`, `PATCH /api/users/:userId`) — Existed before Group B

**Where:**
- Receipt Auditor: lines 36-51 (operations 1-17, only 15-17 are Group B work)
- Policy Gate: lines 109-143 (Finding 3 claims service creation is Group B scope — it's not)

**Why it matters:**
Group B scope was explicitly: "Wire Ava chat/video/agents to orchestrator (replace mock data)" and "Replace mock inbox/authority-queue/mail endpoints with real Supabase queries." Service creation, booking flows, and profile CRUD were Phase 0 deliverables (onboarding form, booking system scaffolding). Auditing them as Group B work creates false scope creep.

**How to verify the fix:**
1. Read Group B spec (W6|W7|W8 plan) — verify actual scope
2. Cross-reference routes.ts git blame: When were booking/service routes added? (Before or during Group B?)
3. Exclude pre-existing routes from audit scope
4. Re-run audits on ONLY Group B additions: approve/deny endpoints, Ops Telemetry Facade, E2E test suites

---

### [SEVERITY: HIGH]
Issue #7: Policy Gate Overstates Law #1 Violation — Stripe Calls Are Phase 0 Debt

**What's wrong:** Policy Gate Finding 3 (lines 109-143) claims "Desktop service creation calls Stripe directly (routes.ts L91-103), bypassing orchestrator and violating Law #1." **This is true but NOT a Group B regression.** Service creation endpoint existed before Group B. Group B did NOT add this endpoint.

**Where:**
- `phase3-groupb-policy-review.md` lines 109-143, 543-581

**Why it matters:**
Law #1 violations are CRITICAL, but this isn't a NEW violation introduced by Group B. It's pre-existing Phase 0 debt (booking/service CRUD scaffolding). Policy Gate marks this as "P0 BLOCKING" for Group B ship, but it should be "CARRY-FORWARD FROM PHASE 0" with separate tracking.

**How to verify the fix:**
1. Git blame `routes.ts` lines 89-116 (service creation endpoint)
2. Verify creation date: Before or during Group B?
3. If before Group B: Remove from Policy Gate blocking issues, add to carry-forward list
4. If during Group B: Confirm this was intentional (unlikely — not in W6|W7|W8 spec)

---

### [SEVERITY: MEDIUM]
Issue #8: Receipt Auditor Exemplar Pattern Is Incorrect — Ops Telemetry Is GREEN, Not YELLOW

**What's wrong:** Receipt Auditor praises Ops Telemetry Facade as "100% compliant exemplar" (lines 829-850) but then shows approval receipts hardcoded to 'yellow' risk tier (desktop routes.ts L808). **Ops Telemetry GET endpoints are GREEN tier (read-only), not YELLOW.** Desktop approval/deny are correctly YELLOW (user confirmation required).

**Where:**
- `phase3-groupb-receipt-audit.md` lines 829-901 ("Ops Telemetry is 100% Compliant", "Desktop Should Adopt These Patterns")
- Lines 72-85 (Ops Telemetry operations table shows ALL as GREEN tier)

**Why it matters:**
The pattern comparison is apples-to-oranges. Ops Telemetry generates GREEN receipts for read operations (correct). Desktop generates YELLOW receipts for approval operations (also correct). Recommendation to "adopt Ops Telemetry patterns" is misleading — Desktop ALREADY uses correct risk tiers for its operations.

**How to verify the fix:**
1. Verify Ops Telemetry risk tiers: admin.py L232 (`risk_tier = "green"` for GET endpoints)
2. Verify Desktop risk tiers: routes.ts L808, L844 (`risk_tier = 'yellow'` for approve/deny)
3. Confirm this is correct mapping: approve/deny = YELLOW (Law #4), read-only admin queries = GREEN
4. Remove false equivalence from Receipt Auditor recommendations

---

### [SEVERITY: MEDIUM]
Issue #9: Missing Edge Case — What If RLS Policies Don't Exist on Target Tables?

**What's wrong:** All 4 reports assume RLS policies exist on `approval_requests`, `suite_profiles`, `services`, `bookings` tables. Policy Gate Bypass Attempt #2 (lines 320-336) correctly identifies: "Vulnerability depends on whether approval_requests has RLS policy" but then doesn't verify this. Security Review assumes RLS enforcement works (line 178) without evidence.

**Where:**
- Policy Gate: lines 329-334 ("Vulnerability depends on RLS policy existence")
- Security Review: lines 161-164 (Law #6: "RLS enforced at DB layer" — no verification)
- Receipt Auditor: lines 421-429 ("RLS policies enforce filtering" — assumed, not verified)

**Why it matters:**
Desktop server uses Drizzle ORM with raw SQL (NOT Supabase client which auto-applies RLS). If target tables lack RLS policies:
- `UPDATE approval_requests` succeeds for ANY tenant (cross-tenant approval)
- `PATCH /api/users/:userId` succeeds for ANY suite (cross-tenant profile modification)
- `DELETE /api/services/:serviceId` succeeds for ANY suite (cross-tenant service deletion)

This is a **CRITICAL** verification gap. All auth claims hinge on RLS existence.

**How to verify the fix:**
1. Query Supabase schema: `SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public' AND tablename IN ('approval_requests', 'suite_profiles', 'services', 'bookings');`
2. Verify each table has at least one RLS policy enabled
3. Verify policies filter by `suite_id = current_setting('app.current_suite_id')`
4. If ANY table lacks RLS: Policy Gate Finding 1-3 are VALID, Security Review is WRONG

---

### [SEVERITY: MEDIUM]
Issue #10: Test Quality Assessment Ignores Untested E2E Scenarios

**What's wrong:** Test Engineer report (lines 316-369) gives W7 verification tests "Grade: A (Excellent)" but admits "No mocking (by design — these are E2E tests)" and "Requires live infrastructure." Then Gate 1 verdict is CONDITIONAL PASS despite ZERO evidence tests were executed.

**Where:**
- `phase3-groupb-gate1.md` lines 336-350 (W7 test quality: A grade), 599-625 (verdict: conditional pass), 646-664 (next steps: execute tests manually)

**Why it matters:**
E2E tests that have never been executed cannot be graded "Excellent." Correct assessment: "Quality: UNKNOWN — Tests are well-structured but unverified. Grade pending live execution." Giving A grade to unexecuted tests creates false confidence.

**How to verify the fix:**
1. Execute W7 verification tests: `pytest tests/e2e/verification/ -v --tb=short`
2. If tests fail: Quality grade drops to C or D (structure good, implementation broken)
3. If tests pass: Grade A is justified
4. Update report with actual execution results, not theoretical assessment

---

## 🚫 MUST CHANGE BEFORE MERGE

Based on the issues above, the following MUST be corrected before accepting Group B as complete:

1. **Receipt Auditor MUST re-scope the audit** to ONLY Group B deliverables (approve/deny endpoints, Ops Telemetry Facade). Remove pre-existing booking/service/profile routes from the gap count. Recalculate coverage: Group B receipt coverage = 3/3 NEW operations (100%), NOT 11/29 total operations (37.9%).

2. **Policy Gate MUST verify RLS policies exist** on `approval_requests`, `suite_profiles`, `services`, `bookings` tables before claiming "Desktop routes are unauthenticated." If RLS exists, Finding 1-3 severity drops from CRITICAL to MEDIUM (advisory — add explicit auth middleware). If RLS missing, findings are VALID CRITICAL.

3. **Test Engineer MUST execute tests before providing verdict.** CONDITIONAL PASS is invalid without execution evidence. Correct verdict: "UNABLE TO VERIFY — Manual execution required. Provide test run output for final verdict."

4. **Security Review MUST reconcile with Policy Gate findings.** Either Policy Gate is wrong (Desktop HAS auth via RLS middleware) OR Security Review is wrong (Desktop has CRITICAL auth gap). These cannot both be true. Perform live exploit test to determine ground truth.

5. **All reports MUST distinguish Group B scope from pre-existing debt.** Booking/service/profile CRUD routes are Phase 0/1 carry-forwards, NOT Group B regressions. Create separate tracking for:
   - **Group B NEW issues:** (approve/deny receipt failures, LiveKit integration gaps)
   - **Carry-forward issues:** (booking/service receipts missing since Phase 0)

6. **Receipt Auditor MUST remove false exemplar comparison.** Ops Telemetry (GREEN tier read-only) vs Desktop approve/deny (YELLOW tier state-changing) are not equivalent patterns. Desktop risk tier assignment is CORRECT for its operations.

7. **Test Engineer MUST downgrade W7/W8 quality grades** from "A (Excellent)" to "PENDING — Well-structured, unverified." Quality cannot be assessed without execution.

---

## ✅ WHAT IS GOOD (brief)

1. **Ops Telemetry Facade architecture is exemplary** — 100% receipt coverage on auth-required endpoints (admin.py L284-299, L362-378), proper JWT validation (L133-158), fail-closed on missing auth (L146-150), DLP payload redaction (L643-664). This is textbook Law compliance.

2. **Desktop approve/deny endpoints correctly implement receipts** — routes.ts L806-811, L842-847 generate receipts with suite_id, correlation_id, risk_tier, actor_type fields. Receipt generation happens BEFORE response (Law #2 compliance). Fail-closed on missing X-Suite-Id header (L788-791, L823-826).

3. **E2E test suite structure is comprehensive** — 40 W7 verification tests (orchestrator workflows, desktop endpoints, mail connection, provider status) + 78 W8 Playwright tests (12 desktop, 5 admin, 3 cross-surface specs). Test organization follows best practices (parametrized tests, proper markers, graceful degradation on missing services).

4. **RLS middleware correctly sets tenant context** — index.ts L46-67 validates JWT, extracts suite_id from user_metadata, sets app.current_suite_id before ANY route execution. Middleware registered at L43 BEFORE routes (correct order). Fail-closed pattern: if JWT invalid, denies request (L742-747 in Fix #9 suggestion).

5. **Onboarding bypass prevention works** — _layout.tsx L76-78 enforces redirect loop until onboarding_completed_at is set. Cannot access /(tabs) routes without completing 3-step onboarding form (onboarding.tsx L71-108). Client-side enforcement is adequate for MVP (server-side enhancement recommended but not blocking).

---

## FINAL VERDICT

**Phase 3 Group B: CONDITIONAL SHIP**

**Conditions:**

1. **EXECUTE ALL TESTS** (blocking) — Cannot ship without verification that 1847 backend tests + 40 W7 tests + 78 W8 tests actually PASS. Test Engineer must provide execution logs.

2. **VERIFY RLS POLICIES EXIST** (blocking) — Query Supabase schema to confirm `approval_requests`, `suite_profiles`, `services`, `bookings` tables have RLS enabled. If missing, Policy Gate findings are VALID CRITICAL.

3. **RE-SCOPE RECEIPT AUDIT** (advisory) — Remove pre-existing booking/service/profile routes from Group B coverage calculation. Correct baseline: Group B receipt coverage = 3/3 NEW operations (100%).

4. **RECONCILE AUTH GAP CONTRADICTION** (blocking) — Security Review claims auth verified, Policy Gate claims no auth. Perform live exploit test (PATCH /api/users/:userId without JWT) to determine ground truth.

5. **DOCUMENT CARRY-FORWARD DEBT** (advisory) — Create separate tracking for pre-existing gaps (booking/service receipts, Stripe direct calls, profile auth). These are Phase 0/1 debt, not Group B regressions.

**Non-blocking recommendations for Phase 3 hardening:**
- Add explicit auth middleware to Desktop routes (Policy Gate Fix #1) — defense-in-depth over RLS-only
- Bind actor_id in Desktop receipts (Policy Gate Fix #2) — improves audit trail
- Server-side onboarding enforcement (Policy Gate Fix #4) — eliminates race condition
- UUID receipt IDs instead of timestamps (Policy Gate Fix #5) — prevents collisions

**Ship readiness:** Group B can ship to staging for integration testing AFTER conditions 1, 2, 4 are verified. Production ship requires all 5 conditions resolved.

---

## MEMORY UPDATE

Recording for future reviews:

### Recurring Anti-Patterns Detected

1. **Scope creep in audits** — Auditors include pre-existing code in "new deliverable" assessments, inflating gap counts. Fix: Always git blame to verify code age before claiming regression.

2. **Untested code given quality grades** — Test Engineer graded E2E tests "Excellent" without execution. Fix: Grade structure separately from execution. Structure = B, Execution = PENDING.

3. **Contradictory agent findings without reconciliation** — Security Review claims auth verified, Policy Gate claims no auth, no cross-validation. Fix: Add "conflict resolution" step where contradictory claims trigger live testing.

4. **Hand-wavy "conditional pass" verdicts** — Claiming PASS without evidence (Test Engineer: "likely maintained 80% coverage"). Fix: Reject conditional verdicts. Only PASS, FAIL, or UNABLE_TO_VERIFY are valid.

### Desktop-Specific Patterns

- **RLS-first auth model:** Desktop relies on database-level RLS instead of explicit auth middleware. Effective IF policies exist, silent fail-open if policies missing. Recommendation: Add middleware for defense-in-depth.

- **Receipt generation pattern:** Desktop uses raw SQL INSERT (routes.ts L806-811) instead of receipt_store.py helpers. Works but inconsistent with orchestrator pattern. Low priority — both approaches comply with Law #2.

- **E2E test dependency on live services:** W7/W8 tests require Desktop (3100), Orchestrator (8000), Domain Rail (Railway) running. Cannot execute in CI without staging environment. Phase 4 work: Add mocked service layer for CI execution.

### Carry-Forward Debt (NOT Group B Issues)

Pre-existing gaps that need separate tracking:
- Booking CRUD receipts missing (Phase 0 debt)
- Service CRUD receipts missing (Phase 0 debt)
- Stripe direct calls bypass orchestrator (Phase 0 debt)
- Profile CRUD receipts missing (Phase 0 debt)
- ElevenLabs TTS receipts missing (Phase 1 debt)

These should be tracked as "Desktop Hardening Backlog" NOT "Group B regressions."

---

**END OF REVIEW**
