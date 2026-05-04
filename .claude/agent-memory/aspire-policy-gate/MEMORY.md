# Policy Gate — Aspire Audit Memory

## Wave 3 — Trust Hub Routes (trust-hub-w3, branch feat/per-tenant-trust-hub-w1-schema)
- Reviewed: `routes/trust_hub.py`, `routes/front_desk.py` (`_validate_cap_token` + `_cap_token_id`), `services/token_service.py`, `workers/trust_onboarding/trust_receipts.py`, `tests/test_trust_hub_routes.py` (66 tests).
- Token enforcement: FULL 6-check HMAC-SHA256 validation in `token_service.validate_token`. Missing key → 401, expired → 401, revoked → 401, scope mismatch → 401, suite_id mismatch → 401, office_id mismatch → 401. All correctly wired via `_validate_cap_token` before any DB/vault call.
- FINDING: `_validate_cap_token` returns HTTP 401 for ALL failure modes (SUITE_MISMATCH, SCOPE_MISMATCH, etc.) — per-spec SUITE/SCOPE mismatches should be 403. Tests use `_mock_validate_token_wrong_scope` which asserts 401, hardcoding this inconsistency into the test suite. Non-blocking; status code semantics only.
- FINDING: Token revocation is in-memory only (`_revoked_tokens: set[str]`). Multi-replica deployments have revocation gap window = token TTL (max 59s). Self-documented in `token_service.py:L379` as THREAT-007/F-HIGH-3. Known deferred item.
- FINDING: Vault orphan leak on EIN-encrypt-success-then-rep-encrypt-failure. EIN vault secret created, then rep vault call fails → 503 returned, but EIN vault secret is never deleted. No compensating cleanup in the failure path. Medium severity.
- Receipt coverage: ALL Yellow paths cut `kyb_collected` receipt with `capability_token_id`, `outcome="success"`. Failure paths (vault unreachable) return 503 BEFORE reaching `cut_trust_receipt` — no receipt is cut on 503 failures. This is the designed behavior (no state change = no receipt). Receipt on failure-path is NOT missing — it's architecturally correct.
- PII guardrails: `_FORBIDDEN_PII_KEYS` enforced at `cut_trust_receipt` entry. Route-layer `redacted_inputs` construction explicitly avoids all PII keys. Verified in tests.
- Dispute uses INSERT semantics on first submit (409 on duplicate) and UPDATE on re-submit, correctly enforced via pre-flight SELECT + UNIQUE constraint. Cannot bypass dispute flow.
- `_cap_token_id()` falls back to `sha256(signature)[:16]` — deterministic but not the UUID token_id if `id` field absent. Acceptable for receipt audit linkage.
- Status-callback: HMAC checked before any DB access. No cap token required (correct — public webhook). Receipt cut only when profile found (no-profile path logs warning + returns 200, no receipt).

## Wave 2 — Trust Onboarding (trust-hub-w2)
- Source .py files absent locally (pyc only); reviewed via plan spec + agent memory. Key findings: 3 missing states in dispatch table, 1 receipt type gap (shaken_submitted), `shaken_trust_product_rejected` receipt type absent from RECEIPT_TYPES, `suspended` state has no handler, `first_name`/`last_name`/`email`/`phone_e164` in `tenant_authorized_reps` are NOT encrypted and could leak via receipt redacted_outputs if state machine logs the rep dict directly. `_POLICY_CACHE` is module-level (keyed by policy type string, NOT tenant) — no cross-tenant pollution risk since the cache is for Twilio-global policy SIDs. Vault-decrypt error branch is an uncovered test path.

## Round 7 — Wave 3.C (policy-gate-r7)
- [round7-findings.md](round7-findings.md) — Full audit: Law #1/3/4/5 verdicts, 4 findings, 5 bypass attempts.

## Key Patterns Found

### Token model
- `verifySecret()` at `agentToolRoutes.ts:281` is a shared-secret check (HMAC-less). Not a scoped capability token per Law #5. This is a known architectural gap — Aspire's Law #5 is NOT fully implemented at the desktop layer. Tokens appear in PlaybookContext dataclass fields but are optional (None default).
- `capability_token_id` and `capability_token_hash` exist in PlaybookContext and are included in receipt emission, but no minting/expiry enforcement code was found in Round 7 changes.

### Fail-closed patterns
- `_shopping_with_backoff()` catches generic Exception and breaks — returns the exception object as `last_result`. Caller checks `isinstance(shopping_result, Exception)` at L929. This correctly degrades (HD still runs). Fail-safe, not fail-open.
- Receipt always emitted on FAILED path (`_emit_playbook_receipt` at L1072). Receipt emission failures are logged-and-swallowed (not blocking), which is a deliberate Law #2 + reliability tradeoff.

### `include_other_stores` is backend-computed gate
- The flag flows: Anam LLM → Anam tool schema → agentToolRoutes.ts L1279 → orchestrator body → server.py L1841 → PlaybookContext.include_other_stores → trades.py L724.
- The flag is NOT a bypass. It just controls whether Google Shopping runs. HD SerpApi always runs.
- `hd_too_far`, `hd_has_stock`, `nearest_store_distance_miles` are backend-computed from Google Places. The LLM (Ava prompt) reads these to make the offer. Backend never decides to surface Lowe's.

### Diagnostic log
- Gated behind `process.env.LOG_TOOL_INVOKE_DIAG === 'true'`. Fires BEFORE verifySecret (L1007). Does NOT log headers — only bodyKeys + rawBodyPreview (200 chars). Risk: body preview can include partial user_address if it appears early in the JSON payload.

### Risk tier
- All Adam invocations (HD + multi-store) are GREEN. No state change. Confirmed in receipt `risk_tier: 'green'` at trades.py L71.

### Tenant isolation gap
- `suite_id` in the invoke path comes from the request body (`body.suite_id`), with fallback to `getDefaultSuiteId()`. No secret-to-tenant binding. THREAT-005 is known and logged. Per-secret tenant binding is deferred (was "Round 6 work" per THREAT-005 comment).
