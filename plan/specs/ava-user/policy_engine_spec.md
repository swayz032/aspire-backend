# Policy Engine Specification

**Source:** Ava User Enterprise Handoff v1.1

## Inputs
- Requested plan (candidate actions)
- User context (suite_id, office_id, roles)
- Skill Pack context (capabilities)
- Tool allowlist (role intersection with skillpack)
- Risk tier classification (green/yellow/red)

## Evaluation order (deterministic)
1. Validate ingress schema.
2. Resolve tenant + actor.
3. Compute candidate tool set.
4. Apply allowlist intersection.
5. Classify risk tier per action.
6. If yellow/red: require approvals.
7. If red: require presence proof.
8. For any execution: require valid capability token.
9. Emit `policy_decision` receipt.

## Policy Requirements (Runtime)

### Evaluation primitives
1. **Tool allowlist intersection**: `(user_role_allowlist intersection skillpack_allowlist)`
2. **Risk tiers**: `green | yellow | red` (no other tier names)
3. **Approvals**: required for `yellow` and `red` actions
4. **Presence**: required for `red` actions (presence_token must be valid)
5. **Capability tokens**: required for any tool execution (scoped, short-lived)

### Fail-closed defaults
- Unknown tool => deny
- Unknown action type => deny
- Missing approvals => deny
- Missing receipts write => disable execution (draft-only)

## Policy outputs
- allow/deny per action
- required approvals (scoped)
- required presence (boolean)
- capability token scope and TTL

## Cross-reference
- PolicyEvaluationRequest contract: to be derived from this spec in Phase 1
- Canonical capability token schema: `plan/schemas/capability-token.schema.v1.yaml`
- Implementation target: Phase 1A (POST /v1/policy/evaluate)
