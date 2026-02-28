# Presence Sessions (Red-Tier Authority)

**Source:** Ava User Enterprise Handoff v1.1

## Goal
Provide server-verifiable evidence that the user is actively present for high-gravity actions (red tier: signing, sending money, irreversible changes).

## Acceptable Proof Patterns
- **Live session token** minted server-side (short TTL) and tied to `suite_id + office_id + session_id`.
- Proof must be **attached to approval** and persisted in receipts.

## Minimum Requirements
- TTL <= 5 minutes
- Nonce bound to `payload_hash`
- Server verifies token signature + freshness

## Minimal Acceptable Implementation
- Server issues a short-lived `presence_token` after a fresh user action (e.g., biometric / re-auth / explicit "I am here").
- Token is bound to `suite_id`, `office_id`, and expires quickly (<= 5 minutes).
- Orchestrator requires the token for red-tier approvals and executions.

## Receipt
Presence checks must emit a receipt (`presence_verified` or `presence_missing`).

## Cross-reference
- Matches CLAUDE.md Law #8 (Hot interaction state for authority moments)
- Capability token schema: `plan/schemas/capability-token.schema.v1.yaml`
- Implementation target: Phase 1B (Intelligence Integration)
