# Gusto Approval Packet (Template)

Goal: package the *minimum reviewer evidence* to speed up production access for Gusto Embedded Payroll.

## Aspire Context
- **Agent**: Milo (Payroll) — RED tier, all payroll operations require explicit authority + approval
- **Orchestrator**: Ava (LangGraph on FastAPI :8000) — Single Brain (Law #1)
- **Gateway**: Express Gateway (`backend/gateway/` on :3100) — webhook ingress, auth, rate limiting
- **Trust Spine**: Receipts, capability tokens, policy engine, approval flows

## What to Include (Attachments)

1. **Architecture diagram** (Ava Orchestrator -> Express Gateway -> Trust Spine -> Gusto) with token storage + tenant boundaries (Law #6).

2. **End-to-end demo script** + screenshots (or short recording).
   - See: `docs/approval-packets/program/gusto/03_demo_script.md`

3. **Strict access compliance proof**
   - Evidence: token audit export showing company-scoped grants (suite_id + office_id isolation).
   - See: `docs/approval-packets/program/gusto/01_strict_access_checklist.md`

4. **Scopes mapping** (principle of least privilege — Law #5)
   - See: `docs/approval-packets/program/gusto/02_scopes_mapping.md`

5. **Webhook mapping + verification**
   - See: `docs/approval-packets/program/gusto/05_webhook_mapping.md`
   - See: `docs/security/webhook_secrets_policy.md`

6. **Security review questionnaire answers** (draft)
   - See: `docs/security/security_review_questionnaire_template.md`

7. **Support readiness**
   - Playbooks: `docs/operations/playbooks/` (auth_revoked, webhook_delayed, payroll_run_failed, transfer_returned)

8. **Incident readiness**
   - Kill switch + replay runbooks: `docs/operations/kill_switch.md`, `docs/operations/replay_trace.md`
   - Incident response: `docs/operations/incident_response.md`

9. **Evidence exports**
   - Receipts from Trust Spine receipt chain (redacted via DLP/Presidio)
   - Provider call logs (redacted)

## Assemble Checklist
Use `docs/approval-packets/submission_checklist.md` and mark every item as complete before submission.
