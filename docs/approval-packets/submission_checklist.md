# Partner Submission Checklist (Generic)

Use this for both Gusto and Plaid Transfer. Treat it as a **release gate**: no submission until every line is checked.

## A. Product + UX
- [ ] End-to-end demo script written and rehearsed
- [ ] Demo environment data/reset instructions
- [ ] Clear user consent screens for data access and high-risk actions (Law #4 — Risk Tiers)
- [ ] Error states + recovery UX for provider outages (degradation ladder: Video -> Audio -> Async Voice -> Text)

## B. Security
- [ ] Token storage documented (encryption at rest, rotation, revocation) — see `docs/security/token_storage_and_rotation.md`
- [ ] Webhook signature verification implemented and tested — see `docs/security/webhook_secrets_policy.md`
- [ ] Tenant isolation tests (RLS) exported and attached — Law #6, 52/52 RLS tests passing
- [ ] Vulnerability management process documented — see `docs/security/vuln_management_checklist.md`
- [ ] Secrets policy (no secrets in logs, DLP/Presidio redaction tests passing) — Gate 5

## C. Governance + Controls (Trust Spine)
- [ ] High-risk actions (RED tier) require explicit approval with authority UX
- [ ] High-risk execution requires capability token (single-use, <60s expiry) — Law #5
- [ ] Kill switch + approvals-only mode tested — see `docs/operations/kill_switch.md`
- [ ] Idempotency keys implemented for all side effects (Gate 3)
- [ ] Worker queue retries + failure handler tested — `backend/orchestrator/services/worker_queue.py`, `failure_handler.py`

## D. Ops
- [ ] On-call / escalation plan exists — see `docs/operations/on_call_minimal.md`
- [ ] Support playbooks for likely failure modes — see `docs/operations/playbooks/`
- [ ] Status page communication template ready — see `docs/operations/status_page_template.md`

## E. Evidence Pack
- [ ] Sample receipts export (redacted) — Trust Spine receipt chain
- [ ] Provider call log export (redacted via DLP/Presidio)
- [ ] Replay bundle produced for at least one full workflow — see `docs/operations/replay_trace.md`
- [ ] Monitoring dashboard screenshots (Grafana: queue depth, error rate, receipt coverage)
