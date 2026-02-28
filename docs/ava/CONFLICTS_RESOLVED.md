# Ava Enterprise Sync -- Conflicts Resolved

**Sync date:** 2026-02-12
**Packages:** Ava Admin v2 (40 files) + Ava User v1.1 (31 files)

---

## CONFLICT 1: Risk Tier Naming (BLOCKING -- RESOLVED)

**Ava User** `ava_result.schema.json`: `"GREEN"` / `"YELLOW"` / `"RED"` (UPPERCASE)
**Canonical** `plan/schemas/receipts.schema.v1.yaml`: `"green"` / `"yellow"` / `"red"` (lowercase)

**Resolution:** Canonical wins. Normalized `plan/contracts/ava-user/ava_result.schema.json` to lowercase during sync. The `risk.tier` enum now reads `["green", "yellow", "red"]`.

**Note:** The original ava_result.example.json also had `"GREEN"` -- normalized to `"green"` in `tests/fixtures/ava-user/examples/ava_result.example.json`.

---

## CONFLICT 2: Capability Token Schema Gap (RESOLVED)

**Ava User**: References capability tokens but doesn't specify signature algorithm.
**Canonical** `plan/schemas/capability-token.schema.v1.yaml`: HMAC-SHA256, 6-check server validation.

**Resolution:** Canonical fills the gap. Ava User's runtime integration references are cross-linked to canonical schema. No actual conflict -- just incompleteness in the handoff package that our canonical schemas already solve.

---

## CONFLICT 3: Receipt Schema Field Alignment (RESOLVED)

**Ava Admin** receipt_chain_spec.md: Lists minimum fields (receipt_id, correlation_id, chain_id, sequence, suite_id, office_id, action_type, risk_tier, tool_used, timestamps, outcome, redaction).
**Canonical** receipts.schema.v1.yaml: 20 fields including previous_receipt_hash, receipt_hash, actor_type, approval_evidence, idempotency_key.

**Resolution:** Canonical is superset. Ava Admin's hash chain spec EXTENDS canonical with `chain_id` + `sequence` + `prev_hash`. These are additive columns, not conflicts. Implementation will add columns via Migration #50 in Phase 1A.

---

## CONFLICT 4: Error Code Taxonomy (RESOLVED)

**Ava User** architecture doc: `SCHEMA_VALIDATION_FAILED`, `APPROVAL_REQUIRED`, `CAPABILITY_TOKEN_REQUIRED`, `TENANT_ISOLATION_VIOLATION`
**Existing**: No formal error code enum yet.

**Resolution:** Adopt ava-user's error codes as the starting point. Extend with additional codes from ava-admin's OpsExceptionCard classes during Phase 1. These become the canonical error taxonomy.

---

## CONFLICT 5: Policy Config Store (RESOLVED)

**Ava User**: References policy engine but doesn't specify where policies are stored.
**Existing**: Supabase tables with RLS (49 migrations, 40 tables).

**Resolution:** Policies will be stored in Supabase `policy_rules` table (to be created in Phase 1, Migration #51). Ava User's PolicyEvaluationRequest schema becomes the API contract for the policy evaluation endpoint (POST /v1/policy/evaluate).

---

## CONFLICT 6: OpsTelemetryFacade -- Doesn't Exist Yet (RESOLVED)

**Ava Admin**: Defines a 6-endpoint read-only API (health, incidents, receipts, provider-calls, outbox, rollouts).
**Existing**: No telemetry facade exists.

**Resolution:** This is a Phase 2 deliverable. The OpenAPI contract is stored at `plan/contracts/ava-admin/ops_telemetry_facade.openapi.yaml` now; implementation happens in Phase 2 when Admin Portal gets wired. No one depends on it yet.

---

## CONFLICT 7: Dual-Path RLS Pattern (RESOLVED)

**Ava Admin** implementation notes: References PostgREST (`auth.uid()`) AND Express server (`current_setting`) patterns.
**Existing**: Our migrations use the `current_setting` pattern exclusively.

**Resolution:** Both patterns documented. PostgREST path activates when/if we expose Supabase directly to clients (not planned for Phase 1-2). Keep `current_setting` as primary. PostgREST path is future-compatible, not a conflict.

---

## Summary

| # | Conflict | Severity | Resolution | Action Taken |
|---|----------|----------|------------|--------------|
| 1 | Risk tier casing | BLOCKING | Canonical wins (lowercase) | Schema normalized |
| 2 | Capability token gap | LOW | Canonical fills gap | Cross-referenced |
| 3 | Receipt field alignment | LOW | Canonical superset + additive columns | Noted for Migration #50 |
| 4 | Error code taxonomy | LOW | Adopt ava-user as starting point | Documented |
| 5 | Policy config store | LOW | Supabase policy_rules table | Noted for Migration #51 |
| 6 | Telemetry facade missing | LOW | Phase 2 deliverable | OpenAPI stored now |
| 7 | Dual-path RLS | LOW | Document both, current_setting primary | Documented |

**All 7 conflicts resolved. Zero governance violations. Zero blocking issues remaining.**
