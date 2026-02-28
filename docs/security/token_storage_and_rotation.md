# Token Storage + Rotation Policy

## Storage Requirements
- All provider OAuth tokens and API keys are stored **encrypted at rest** in Supabase (State Layer).
- Tokens are scoped to `suite_id` + `office_id` + `provider` (Law #6 — Tenant Isolation).
- Tokens are **never logged** in any form — receipts reference `capability_token_id` (hash), not the token value (Law #9).
- Access to stored tokens is restricted to the LangGraph orchestrator and tool executor services only.

## Revocation
- Per-tenant, per-provider revocation via Admin API or kill switch.
- Revocation generates an immutable receipt (Law #2).
- Revoked tokens are immediately invalidated — fail closed on any subsequent use (Law #3).

## Rotation Policy

### Scheduled Rotation
- **OAuth refresh tokens**: Rotated automatically on each token refresh cycle (provider-dependent).
- **Webhook secrets**: Rotated every 90 days or per provider recommendation.
- **Receipt signing keys**: Rotated using `kid`-based scheme — new key ID issued, old key retained for verification of existing receipts.
- **S2S HMAC secrets** (e.g., Domain Rail): Rotated every 90 days via Railway environment variables.

### Emergency Rotation
- Triggered by: suspected compromise, auth revocation, security incident.
- Procedure:
  1. Set affected provider to `APPROVAL_ONLY` via kill switch.
  2. Rotate the compromised credential.
  3. Verify new credential with a GREEN-tier read-only test.
  4. Restore normal execution mode.
  5. Generate receipt for the rotation event.

## Receipt Signing Keys
- Use `kid` (key ID) based rotation for receipt chain integrity.
- Old signing keys are retained read-only for hash chain verification of historical receipts.
- New receipts use the latest `kid`.
- Verification service (`backend/orchestrator/services/receipt_chain.py`) supports multi-key validation.

## Capability Tokens (Law #5)
- Short-lived: <60s expiry.
- Scoped: `suite_id` + `office_id` + `tool` + `action`.
- Server-verified only — no client-side trust.
- Minted exclusively by the LangGraph orchestrator (Law #1).
- Stored in orchestrator state for execute-node validation.
