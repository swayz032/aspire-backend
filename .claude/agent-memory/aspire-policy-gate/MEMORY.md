# Policy Gate — Aspire Audit Memory

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
