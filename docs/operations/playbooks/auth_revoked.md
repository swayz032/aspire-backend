# Support Playbook: Authorization Revoked

## Symptoms
- Provider returns `auth_invalid` / HTTP 401
- Ava orchestrator receives `denied` outcome from tool executor
- Receipt generated with `reason_code: auth_revoked`

## Affected Agents
- **Finn** (Money Desk) — Moov/Plaid tokens
- **Milo** (Payroll) — Gusto tokens
- **Quinn** (Invoicing) — Stripe Connect tokens
- **Teressa** (Books) — QuickBooks tokens
- **Clara** (Legal) — PandaDoc tokens

## Steps

1. **Immediate mitigation**: Set `execution_controls` to `APPROVAL_ONLY` for the affected provider and tenant (suite/office).
   - Use kill switch via Admin API: `POST /admin/kill-switch` with `{ provider, suite_id, mode: "APPROVAL_ONLY" }`
   - Receipt is auto-generated for the mode change (Law #2).

2. **Prompt reconnect flow**: Notify the suite owner via Ava (YELLOW tier — requires user confirmation).
   - Ava sends a reconnect prompt through the appropriate channel (voice/text/async).
   - The user must re-authorize via OAuth to restore the provider connection.

3. **Record receipt + privileged audit entry**:
   - Verify the `auth_revoked` receipt exists in the Trust Spine receipt chain.
   - Ensure the receipt includes: `correlation_id`, `trace_id`, `suite_id`, `office_id`, `provider`, `reason_code`.
   - Log a privileged audit entry for the revocation event.

4. **Verify restoration**: After reconnect, run a GREEN-tier read-only test against the provider to confirm tokens are valid.

## Escalation
- If reconnect fails repeatedly, escalate to P1 (provider outage path).
- If the revocation was triggered by suspicious activity, escalate to incident response (`docs/operations/incident_response.md`).
