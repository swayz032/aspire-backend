# ACH Returns SOP — Plaid Transfer

## Standard Operating Procedure

### Step 1: Verify Webhook Signature + Ingest Idempotently
- Express Gateway (`backend/gateway/`) verifies the Plaid webhook signature.
- Idempotency key ensures the return event is processed exactly once.
- If signature verification fails: reject, log security receipt, alert on-call (Law #3).

### Step 2: Record Return Receipt with Stable Taxonomy Code
- Finn (Money Desk) processes the return and emits a Trust Spine receipt.
- Receipt includes: ACH return code (R01, R02, R03, etc.), `trace_id`, `suite_id`, `office_id`, original `transfer_id`.
- Return codes mapped to Aspire internal error taxonomy for consistent handling.
- Receipt is immutable — corrections are new receipts (Law #2).

### Step 3: Notify Customer + Provide Next Steps
- Ava orchestrator routes notification via Eli (Inbox) or Sarah (Front Desk) — YELLOW tier.
- Notification includes: transfer amount, human-readable return reason, required actions.
- PII redacted from logs (Gate 5, DLP/Presidio). Customer-facing message uses safe, non-technical language.

### Step 4: Apply Policy (Escalating Controls)
- Ava orchestrator evaluates the return against the suite's risk profile (Law #1):
  - **First return**: Record, notify, monitor. No automatic restriction.
  - **Repeated returns (3+ in 30 days)**: Reduce transfer limits for the affected office.
  - **Fraud-indicative returns (R10 Unauthorized, R29 Corporate Not Authorized)**: Set `execution_controls` to `APPROVAL_ONLY` immediately.
  - **Severe pattern**: Set `execution_controls` to `DISABLED`. Engage compliance.
- All policy changes generate receipts with `action_type: policy.update` (Law #2).

### Step 5: Escalate High-Value or Repeated Returns
- Returns >$10,000: Automatic P0 escalation, engage incident response.
- Fraud-indicative return codes: P0, disable transfers, engage compliance counsel.
- 3+ returns in 7 days for same suite: P1, root cause investigation required.
- See: `docs/operations/incident_response.md` for full escalation procedure.
