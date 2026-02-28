# Implementation Worklist (Partner Approval + Ops)

## P0 — Required for partner reviews (do first)
1. Implement webhook ingestion endpoints for each provider
   - Verify signature on raw body bytes
   - Dedupe by (provider, event_id)
   - Emit receipt/event and link trace_id
   - Update state via Trust Spine RPCs

2. Implement Provider Adapter Contract
   - preflight/execute/simulate/classify_error/redact
   - enforce idempotency key usage
   - store redacted request/response + stable error taxonomy

3. Generate approval evidence exports
   - SQL scripts to export receipts/provider logs by trace_id
   - CI outputs: RLS tests, redaction tests
   - Replay bundle export instructions

## P1 — Ops readiness
4. Support playbooks (payroll failures, transfer returns, auth revoked, webhook delays)
5. Incident response runbooks + kill-switch procedures (execution_controls)

## P2 — Program design docs (reviewer narrative)
6. Plaid: platform vs originator decision + funds flow + KYB/KYC + returns
7. Gusto: strict access checklist + demo script + pilot plan

## Acceptance criteria (10/10)
- A reviewer can follow the packet and verify:
  - strict access (Gusto) / auth->create ordering (Plaid)
  - kill switch works
  - receipts exist and are traceable
  - webhooks are verified and idempotent
