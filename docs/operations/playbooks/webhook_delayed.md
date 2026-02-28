# Support Playbook: Webhook Delayed

## Symptoms
- Provider webhooks not received within expected window
- State appears stale (e.g., payroll status not updating, transfer status stuck)
- Express Gateway (`backend/gateway/`) shows no recent webhook ingress for the provider
- No signature verification failures in logs (webhooks simply not arriving)

## Affected Agents
- **Milo** (Payroll) — Gusto webhooks (payroll status, employee changes)
- **Finn** (Money Desk) — Plaid Transfer webhooks (transfer status, returns)
- **Quinn** (Invoicing) — Stripe webhooks (payment status, invoice events)

## Steps

1. **Check Express Gateway logs** for webhook ingress:
   - Look for recent entries on the webhook routes in `backend/gateway/`.
   - Check for signature verification failures (may indicate secret rotation needed).
   - Check correlation IDs flowing through the system (Gate 2).

2. **Check provider status page** (manual verification):
   - Gusto: https://status.gusto.com
   - Plaid: https://status.plaid.com
   - Stripe: https://status.stripe.com
   - If provider reports an outage, switch to monitoring mode and wait.

3. **Reconcile by polling provider API** (read-only, GREEN tier):
   - If the provider allows polling, use the appropriate skill pack agent to fetch current state.
   - Ava orchestrator issues a GREEN-tier capability token for read-only access.
   - Compare polled state against local state to identify gaps.

4. **Emit reconciliation receipts**:
   - For each state discrepancy found, emit a reconciliation receipt via the Trust Spine.
   - Receipt must include: `action_type: reconciliation`, `reason_code: webhook_delayed`, `trace_id`.
   - All state corrections flow through the normal orchestrator pipeline (Law #1).

## Escalation
- If webhooks remain delayed >4 hours, escalate to P1.
- If financial state is inconsistent (transfers/payments), escalate to P0 and engage incident response.
