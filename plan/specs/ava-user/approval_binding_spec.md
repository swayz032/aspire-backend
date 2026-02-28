# Approval Binding Specification

**Source:** Ava User Enterprise Handoff v1.1

## Approval request payload
- Must be specific and human-readable.
- Must include `payload_hash` (SHA-256 of canonical JSON of the exact execution payload).
- Must include `expires_at`.

## Approval binding
Approval must bind to:
- `suite_id`, `office_id`
- `request_id`
- `payload_hash`
- `policy_version`

## Replay defense
- Reject approvals with mismatched `payload_hash`.
- Reject expired approvals.
- Reject approvals reused across different request_id.

## Cross-reference
- Prevents the "approve then swap payload" attack
- AvaOrchestratorRequest: `plan/contracts/ava-user/ava_orchestrator_request.schema.json`
- Implementation target: Phase 1B (Intelligence Integration)
