# Phase 2 Quality Review — Aspire Orchestrator Brain Layer

**Reviewer:** Output Critic
**Date:** 2026-02-14
**Scope:** Phase 2 implementation (Brain Layer, 11+ skill packs, admin API, state machines, infrastructure)
**Files Reviewed:** 25+ new files across orchestrator, skillpacks, Brain Layer services

---

## 🔴 TOP 10 ISSUES (most severe first)

### [SEVERITY: CRITICAL]
**Issue #1: Missing Finn Finance Manager Skill Pack Implementation**

**What's wrong:** The `skill_pack_manifests.yaml` registers `finn_finance_manager` (lines 123-135) with 5 actions, but no implementation file exists at `src/aspire_orchestrator/skillpacks/finn_finance_manager.py`. The skillpacks directory contains 11 Python files but none for Finn Finance Manager.

**Where:**
- Registered: `config/skill_pack_manifests.yaml` lines 123-135
- Missing: `src/aspire_orchestrator/skillpacks/finn_finance_manager.py`

**Why it matters:** Critical gap. The registry claims 12 skill packs are available, but only 11 are implemented. Any Brain Layer classification routing to `finn_finance_manager` will fail at runtime with import error. This violates Law #1 (orchestrator must have a valid execution path) and Law #3 (fail closed).

**How to verify the fix:**
1. `ls src/aspire_orchestrator/skillpacks/*.py | wc -l` must equal 13 (11 existing + finn_finance_manager + __init__)
2. Import test: `python -c "from aspire_orchestrator.skillpacks.finn_finance_manager import FinnFinanceManagerSkillPack"`
3. Runtime test: Intent classification → `finn_finance_manager` → execute node must succeed (not crash)

---

### [SEVERITY: CRITICAL]
**Issue #2: Admin API Missing /admin/proposals Contract Endpoints**

**What's wrong:** The OpenAPI contract at `plan/contracts/ava-admin/ops_telemetry_facade.openapi.yaml` defines 9 endpoints, but the implementation in `routes/admin.py` only implements 9 (health, incidents x2, receipts, provider-calls, outbox, rollouts, proposals/pending, proposals/{id}/approve). HOWEVER, the contract does NOT define `/admin/proposals/pending` or `/admin/proposals/{id}/approve` endpoints in the paths section (lines 130-341). The implemented endpoints (lines 775-964 in admin.py) have NO matching contract spec.

**Where:**
- Contract: `plan/contracts/ava-admin/ops_telemetry_facade.openapi.yaml` paths section (lines 130-341)
- Implementation: `src/aspire_orchestrator/routes/admin.py` lines 775-964

**Why it matters:** Contract-implementation mismatch. The admin portal will call `/admin/proposals/*` endpoints expecting specific schemas, but the contract doesn't document them. This breaks OpenAPI-driven client generation and creates undefined behavior for approval flows. Violates spec compliance (production readiness gate).

**How to verify the fix:**
1. Add `/admin/proposals/pending` and `/admin/proposals/{proposal_id}/approve` to `ops_telemetry_facade.openapi.yaml` paths section
2. Define request/response schemas for ChangeProposal object
3. Run OpenAPI validator: `npx @apidevtools/swagger-cli validate ops_telemetry_facade.openapi.yaml`
4. Generate TypeScript client and verify no missing types

---

### [SEVERITY: HIGH]
**Issue #3: Policy Matrix Action Count Mismatch (53 vs 61 expected)**

**What's wrong:** User context states policy_matrix.yaml should have ~61 total actions (31 GREEN, 21 YELLOW, 9 RED). Actual counts from grep:
- Total action definitions: **53**
- GREEN tier: **32** (expected 31) — 1 extra
- YELLOW tier: **21** (matches expected)
- RED tier: **9** (matches expected)
- **TOTAL: 62 action entries but only 53 unique actions**

The discrepancy is caused by duplicate `risk_tier` declarations within multi-line action definitions. The actual unique action count is 53, not 61.

**Where:** `config/policy_matrix.yaml` lines 39-686

**Why it matters:** If the expected count is 61 but only 53 actions exist, there are 8 missing action definitions. This could mean:
1. Phase 2 actions weren't added to policy matrix (Brain Layer classify → route will fail)
2. Skill pack manifests reference actions that don't exist (routing denial)
3. Action coverage gaps in testing (missing TCs for 8 actions)

**How to verify the fix:**
1. Count unique actions: `grep -E '^  [a-z_]+\.[a-z_]+:' policy_matrix.yaml | wc -l` must equal 61
2. Cross-reference with skill pack manifest actions: All actions in manifests must exist in policy_matrix
3. Run policy engine test: `pytest tests/test_policy_engine.py::test_all_actions_defined`
4. Verify Brain Layer classify can route to all 61 actions without "unknown action" denial

**Note:** This may be a cosmetic issue if 53 is the correct count and user context is outdated. Needs clarification.

---

### [SEVERITY: HIGH]
**Issue #4: Skill Pack Manifests Count Mismatch (12 vs 11 implementations)**

**What's wrong:** `skill_pack_manifests.yaml` defines 12 skill packs (lines 34-213), but only 11 Python implementations exist in `src/aspire_orchestrator/skillpacks/`:
- Registered: 12 (sarah, eli, quinn, nora, adam, tec, finn_finance_manager, finn_money_desk, milo, teressa, clara, mail_ops)
- Implemented: 11 (missing finn_finance_manager)

**Where:**
- Manifest: `config/skill_pack_manifests.yaml` lines 34-213
- Directory: `src/aspire_orchestrator/skillpacks/` (11 .py files)

**Why it matters:** Same as Issue #1 — runtime failure when Brain Layer routes to `finn_finance_manager`. This affects:
- Intent classification → skill router → execute (crash on import)
- A2A dispatch to Finn (delegation failure)
- Finance snapshot/exceptions/proposal actions (all fail)

**How to verify the fix:**
1. Implement `finn_finance_manager.py` following the SkillPackResult pattern
2. Register in `skillpacks/__init__.py`
3. Add unit tests: `tests/test_finn_finance_manager.py`
4. Run skill router test: `pytest tests/test_skill_router.py::test_route_to_finn_finance_manager`

---

### [SEVERITY: MEDIUM]
**Issue #5: Graph.py Node Count Claim vs Implementation (11 nodes claimed, actually 11)**

**What's wrong:** False positive — graph.py header comment (line 3) claims "11 nodes" and implementation matches:
- intake, safety_gate, classify, route, policy_eval, approval_check, token_mint, execute, receipt_write, qa, respond

This is CORRECT. No issue here, just documenting for completeness.

**Where:** `src/aspire_orchestrator/graph.py` lines 1-356

**Why it matters:** N/A — verified correct.

**How to verify:** Count nodes in `build_orchestrator_graph()`:
```bash
grep 'graph.add_node' graph.py | wc -l
# Expected: 11
```

---

### [SEVERITY: MEDIUM]
**Issue #6: TODO Comment in Safety Gate (NeMo Guardrails Not Integrated)**

**What's wrong:** Line 47 in `nodes/safety_gate.py` has a TODO comment:
```python
# TODO (W2-03): Integrate NeMo Guardrails
```

This indicates safety_gate_node is using a stub implementation (always returns `safety_passed=True`), which bypasses safety checks in production.

**Where:** `src/aspire_orchestrator/nodes/safety_gate.py` line 47

**Why it matters:** Security gap. Without NeMo Guardrails:
- No prompt injection detection
- No PII leakage prevention in user utterances
- No toxic/harmful content filtering
- Safety gate is a no-op (always allows)

This blocks production Gate 5 (security review) until fixed.

**How to verify the fix:**
1. NeMo Guardrails integration implemented (LangGraph callable)
2. Safety gate test: `pytest tests/test_safety_gate.py::test_prompt_injection_blocked`
3. Evil test: Submit prompt injection → expect `safety_passed=False` + denial receipt
4. Remove TODO comment after integration

---

### [SEVERITY: MEDIUM]
**Issue #7: STUB Implementations in Tool Executor (7 tools live, rest stubbed)**

**What's wrong:** `tool_executor.py` lines 9, 631, 807 indicate most tools return stub success (`reason_code="EXECUTED_STUB"`). Only 7 tools are implemented:
- domain_rail_client (4 domain tools)
- search_router (2 search tools)
- internal tools (1 tool)

All other tools in `skill_pack_manifests.yaml` (36+ tools across 16 providers) return fake success without actual execution.

**Where:**
- `src/aspire_orchestrator/services/tool_executor.py` lines 9, 631, 807
- `tests/test_tool_executor.py` line 242 (expects `EXECUTED_STUB`)

**Why it matters:** Partial implementation risk. Skill packs will appear to succeed but produce no real output. This creates:
- False confidence in test results (tests pass with stubs)
- Receipt pollution (fake success receipts)
- Integration gaps (no actual Stripe/Gusto/PandaDoc calls)

**How to verify the fix:**
1. Implement real tool clients for each provider (or mark as Phase 3 deferred)
2. Update tests to expect `EXECUTED` not `EXECUTED_STUB`
3. Add integration tests with sandbox provider accounts
4. Document stub vs live tool status in `docs/tool-coverage.md`

---

### [SEVERITY: MEDIUM]
**Issue #8: Puppeteer and S3 Client Stub Warnings**

**What's wrong:** Both `providers/puppeteer_client.py` and `providers/s3_client.py` have prominent STUB warnings in their headers (lines 8-9 in both files).

**Where:**
- `src/aspire_orchestrator/providers/puppeteer_client.py` line 8
- `src/aspire_orchestrator/providers/s3_client.py` line 8

**Why it matters:** Tec Documents skill pack (document.generate, document.preview, document.share) will fail to produce real PDFs or presigned URLs. This affects:
- Adam Research → RFQ generation (no PDF output)
- Quinn → Invoice PDF generation
- Clara → Contract PDF generation

All document-based workflows are broken until these providers are implemented.

**How to verify the fix:**
1. Implement Puppeteer client using Playwright/Puppeteer
2. Implement S3 client using boto3
3. Integration test: Generate PDF → upload to S3 → verify presigned URL works
4. Remove STUB warnings from file headers

---

### [SEVERITY: LOW]
**Issue #9: Admin API Uses In-Memory Stores (Phase 2 Temporary Pattern)**

**What's wrong:** Admin API (`routes/admin.py`) uses thread-locked in-memory dicts for incidents, provider_calls, rollouts, proposals (lines 48-53). This is explicitly marked as "Phase 2 — will be replaced by Supabase in Phase 3" (line 45).

**Where:** `src/aspire_orchestrator/routes/admin.py` lines 45-100

**Why it matters:** Cosmetic for Phase 2, blocking for production. In-memory stores:
- Don't survive orchestrator restarts (all admin data lost)
- Don't scale across multiple orchestrator instances
- No persistence for incident timelines, rollout history, change proposals

**How to verify the fix:**
1. Defer to Phase 3 (document in Phase 2 ship notes)
2. Add Supabase tables: `admin_incidents`, `admin_proposals`, `admin_rollouts`, `admin_provider_calls`
3. Migrate admin.py to use `supabase_client` instead of `_store_lock` dicts
4. Add RLS policies for admin-only access

---

### [SEVERITY: LOW]
**Issue #10: QA Meta-Receipt Not Chain-Hashed (Intentional Design)**

**What's wrong:** `graph.py` lines 152-156 document that QA meta-receipt is stored separately and NOT appended to `pipeline_receipts` because it would break chain integrity.

This is **intentional**, not a bug, but worth documenting for audit clarity.

**Where:** `src/aspire_orchestrator/graph.py` lines 149-157

**Why it matters:** Audit trail completeness. QA verification receipts are emitted but not part of the hash-chain. This could be perceived as an audit gap (Law #2 compliance question).

**How to verify this is acceptable:**
1. Verify QA meta-receipt is still persisted to receipt store (not dropped)
2. Verify respond node includes `qa_meta_receipt` in response payload
3. Document in `docs/architecture/receipt-chain.md` why QA receipts are separate
4. Confirm with auditor that separate QA receipts are acceptable for governance

---

## 🚫 MUST CHANGE BEFORE MERGE

**BLOCKING ISSUES (CRITICAL/HIGH severity):**

1. **Finn Finance Manager skill pack MUST be implemented** — registry claims it exists but import will fail (Issue #1)
2. **Admin API contract MUST define /admin/proposals/* endpoints** — client generation broken (Issue #2)
3. **Policy matrix action count MUST be verified** — either add 8 missing actions or correct user context claim (Issue #3)
4. **Skill pack manifest count MUST match implementations** — 12 registered, 11 exist (same as Issue #1)

**DEFER TO PHASE 3 (documented, not blocking):**

5. **Safety gate NeMo integration** — blocks production Gate 5, but acceptable stub for Phase 2 dev (Issue #6)
6. **Tool executor stub coverage** — blocks production Gate 4 (soak test), but acceptable for Phase 2 (Issue #7)
7. **Puppeteer/S3 client stubs** — blocks document workflows, defer to provider integration phase (Issue #8)
8. **Admin API in-memory stores** — explicitly marked Phase 2 temporary (Issue #9)
9. **QA meta-receipt chain separation** — intentional design, document for auditors (Issue #10)

---

## ✅ WHAT IS GOOD

1. **Skill pack pattern consistency:** All 11 implemented skill packs follow the same SkillPackResult pattern (success, data, receipt, error, approval_required, presence_required). No copy-paste divergence detected across adam_research, quinn_invoicing, clara_legal, eli_inbox.

2. **Law #2 receipt coverage:** Every skill pack method emits receipts for success, failure, AND denial. Receipt building is consistent across all packs (_emit_receipt / _make_receipt helpers with inputs_hash, correlation_id, suite_id, office_id, actor).

3. **Graph.py node structure:** 11-node LangGraph matches spec exactly. Conditional routing logic is clean (safety → classify → route → policy → approval → token → execute → receipt → qa → respond). Backwards compatibility preserved (utterance not set → skip classify/route).

4. **Binding fields enforcement:** Quinn, Clara, and all YELLOW/RED skill packs correctly check binding fields before marking approval_required. This implements approve-then-swap defense (Law #4 compliance).

5. **Admin API auth fail-closed:** All admin endpoints check `_require_admin()` first, return 401 + denial receipt on missing/invalid token. Dev mode bypass is clearly marked (lines 144-146). Production JWT validation path exists (lines 148-163).

---

## SUMMARY

**Ship-ready status:** CONDITIONAL SHIP
**Blocking issues:** 2 CRITICAL (Finn Finance Manager missing, admin contract mismatch)
**Deferred to Phase 3:** 5 MEDIUM/LOW (NeMo, tool stubs, S3/Puppeteer, admin stores, QA receipts)

**Total issues found:** 10 (2 CRITICAL, 2 HIGH, 4 MEDIUM, 2 LOW)
**False positives:** 1 (Issue #5 — node count is correct)

**Recommendation:** Fix Issues #1 and #2 before merge. Document Issues #6-9 as Phase 3 work. Issue #10 requires architecture decision (is QA meta-receipt separation acceptable for audit compliance?).

---

## VERIFICATION CHECKLIST

Before ship:

- [ ] `finn_finance_manager.py` implemented and importable
- [ ] `ops_telemetry_facade.openapi.yaml` updated with `/admin/proposals/*` paths
- [ ] Policy matrix action count verified (53 vs 61 clarified)
- [ ] All skill pack imports succeed: `python -c "from aspire_orchestrator.skillpacks import *"`
- [ ] Brain Layer classify → route → execute end-to-end test passes for all 12 skill packs
- [ ] Admin API OpenAPI validation passes
- [ ] Document Phase 3 deferred work (NeMo, tool coverage, S3/Puppeteer, admin persistence)

---

**Reviewed by:** Output Critic
**Next step:** Fix CRITICAL issues, re-run verification checklist, update ship verdict
