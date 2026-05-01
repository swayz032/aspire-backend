# Round 7 Policy Gate — wave3-policy-gate-r7

## Laws audited: #1, #3, #4, #5
## Files reviewed:
- backend/orchestrator/src/aspire_orchestrator/services/adam/playbooks/trades.py (L400-1158)
- backend/orchestrator/src/aspire_orchestrator/services/adam/schemas/playbook_context.py
- backend/orchestrator/src/aspire_orchestrator/server.py (L1800-1855)
- Aspire-desktop/server/agentToolRoutes.ts (L1-1430)

## Verdicts
- Law #1 (Single Brain): PASS
- Law #3 (Fail Closed): PASS-WITH-FOLLOWUPS — one LOW finding (shopping exception break semantics)
- Law #4 (Risk Tiers): PASS — all new paths are GREEN
- Law #5 (Capability Tokens): PRE-EXISTING GAP — shared-secret only, no scoped token minting/expiry. Not introduced by Round 7.

## Findings
1. MEDIUM — `include_other_stores` accepted as raw body value without type coercion in server.py. If Anam sends `"true"` (string) instead of `true` (bool), Python `val is not None` passes it through and Python's bool("true") == True, BUT the dataclass type annotation is `bool`. If the value is a string, trades.py L724 `(not voice_path) or include_other_stores` will evaluate string truthiness (non-empty string = True), silently enabling multi-store for any truthy string.
2. LOW — diagnostic log fires before verifySecret. 200-char body preview could expose partial address if user_address appears in first 200 chars of JSON body. Temporary by design; must roll back within 24h.
3. PRE-EXISTING MEDIUM — `suite_id` binding is body-supplied with no per-secret tenant pinning (THREAT-005, logged). Not new to Round 7.
4. PRE-EXISTING HIGH — Law #5 shared-secret is not a scoped capability token with expiry. Not introduced by Round 7 but worth repeating.
