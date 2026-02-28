# Partner Submission Checklist (Generic)

Use this for both Gusto and Plaid Transfer. Treat it as a **release gate**: no submission until every line is checked.

## A. Product + UX
- [ ] End-to-end demo script written and rehearsed
- [ ] Demo environment data/reset instructions
- [ ] Clear user consent screens for data access and high-risk actions
- [ ] Error states + recovery UX for provider outages

## B. Security
- [ ] Token storage documented (encryption at rest, rotation, revocation)
- [ ] Webhook signature verification implemented and tested
- [ ] Tenant isolation tests (RLS) exported and attached
- [ ] Vulnerability management process documented
- [ ] Secrets policy (no secrets in logs, redaction tests passing)

## C. Governance + Controls (Trust Spine)
- [ ] High-risk actions require approval
- [ ] High-risk execution requires capability token (single-use, expiring)
- [ ] Kill switch + approvals-only mode tested
- [ ] Idempotency keys implemented for all side effects
- [ ] Outbox retries + DLQ tested

## D. Ops
- [ ] On-call / escalation plan exists
- [ ] Support playbooks for likely failure modes
- [ ] Status page communication template ready

## E. Evidence pack
- [ ] Sample receipts export (redacted)
- [ ] Provider call log export (redacted)
- [ ] Replay bundle produced for at least one full workflow
- [ ] Monitoring dashboard screenshots (queue depth, error rate)
