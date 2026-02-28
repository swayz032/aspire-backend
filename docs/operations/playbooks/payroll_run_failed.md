# Support Playbook: Payroll Run Failed

## Symptoms
- Milo (Payroll agent) reports execution failure from Gusto API
- Receipt generated with `outcome: failed`, `action_type: payroll.run`
- Risk tier: RED (payroll execution is always RED tier)

## Affected Agents
- **Milo** (Payroll) — primary handler via Gusto integration
- **Ava** (Orchestrator) — decision authority for retry/escalation

## Steps

1. **Inspect receipt + provider call log** (redacted):
   - Query the Trust Spine receipt chain for the `trace_id` of the failed payroll run.
   - Check `receipt.reason_code` and `receipt.redacted_outputs` for the provider error.
   - Verify the receipt includes all required fields (Law #2): `correlation_id`, `suite_id`, `office_id`, `capability_token_id`, `approval_evidence`.

2. **If retryable** (e.g., transient provider error, timeout):
   - Ava orchestrator decides whether to retry (Law #1 — Single Brain).
   - Retry uses the same idempotency key to prevent duplicate payroll execution (Gate 3).
   - Worker queue in `backend/orchestrator/services/worker_queue.py` handles retry with exponential backoff.
   - New receipt generated for the retry attempt.

3. **If non-retryable** (e.g., validation error, insufficient funds, invalid employee data):
   - Do NOT retry — fail closed (Law #3).
   - Create an A2A task item for human resolution via the Admin API.
   - Receipt generated with `outcome: failed`, `reason_code` from provider error taxonomy.

4. **Notify customer with next steps**:
   - Ava sends notification through Sarah (Front Desk) or Eli (Inbox) — YELLOW tier.
   - Include: what failed, whether it can be retried, what action the user needs to take.
   - Never expose raw provider error details or PII in customer-facing messages (Gate 5).

## Escalation
- Failed payroll within 24h of pay date: P0, engage incident response immediately.
- Repeated failures for same suite: investigate provider connection, check `auth_revoked` playbook.
- If payroll was partially executed (some employees paid, others not): P0, coordinate with Gusto support.
