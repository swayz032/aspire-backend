# Support Playbook: Transfer Returned

## Symptoms
- Plaid Transfer webhook received with return event
- Finn (Money Desk) processes the return notification
- Receipt generated with `action_type: transfer.return`, `outcome: completed`
- Risk tier: RED (money movement events are always RED tier)

## Affected Agents
- **Finn** (Money Desk) — primary handler via Moov/Plaid integration
- **Ava** (Orchestrator) — decision authority for policy actions

## Steps

1. **Verify webhook signature + idempotent ingest**:
   - Express Gateway (`backend/gateway/`) validates the Plaid webhook signature.
   - Idempotency check ensures the return event is processed exactly once.
   - If signature verification fails, reject the webhook and log a security receipt.

2. **Record return receipt with stable code**:
   - Emit a Trust Spine receipt with the ACH return code (e.g., R01, R02, R03).
   - Receipt must include: `correlation_id`, `trace_id`, `suite_id`, `office_id`, original `transfer_id`, return reason code.
   - Map the return code to Aspire's internal error taxonomy (`backend/orchestrator/services/finn_money_desk.py`).
   - Receipt is immutable (Law #2) — corrections are new receipts, not updates.

3. **Notify customer**:
   - Ava orchestrator routes notification through Eli (Inbox) or Sarah (Front Desk) — YELLOW tier.
   - Include: transfer amount, return reason (human-readable), next steps.
   - Redact sensitive financial details from logs (Gate 5, DLP via Presidio).

4. **Apply policy** (Ava orchestrator decides — Law #1):
   - Based on return frequency and severity, apply escalating controls:
     - **First return**: Record, notify, monitor.
     - **Repeated returns**: Reduce transfer limits for the affected office.
     - **Fraud-indicative returns**: Set `execution_controls` to `APPROVAL_ONLY` for money movement.
     - **Severe/repeated fraud**: Set `execution_controls` to `DISABLED` for the provider.
   - All policy changes generate receipts with `action_type: policy.update`.

## Escalation
- High-value return (>$10,000): P0, engage incident response.
- Fraud-indicative return codes (R10, R29): P0, disable transfers immediately, engage compliance.
- Multiple returns in 24h for same suite: P1, investigate root cause.
