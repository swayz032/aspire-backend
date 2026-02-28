# Plaid Transfer Approval Packet (Template)

Goal: make your Transfer application and review straightforward by supplying program details + operational controls.

## Aspire Context
- **Agent**: Finn (Money Desk) — RED tier, all money movement operations require explicit authority + approval
- **Orchestrator**: Ava (LangGraph on FastAPI :8000) — Single Brain (Law #1)
- **Gateway**: Express Gateway (`backend/gateway/` on :3100) — webhook ingress, auth, rate limiting
- **Trust Spine**: Receipts, capability tokens, policy engine, approval flows
- **State machines**: Payment state machine (`backend/orchestrator/services/state_machines/payment.py`) governs transfer lifecycle

## What to Include (Attachments)

1. **Platform vs Originator decision**
   - See: `docs/approval-packets/program/plaid-transfer/01_platform_vs_originator_decision.md`

2. **Funds flow diagrams**
   - See: `docs/approval-packets/program/plaid-transfer/02_funds_flow_diagrams.md`

3. **Verification + risk workflow** (KYB/KYC + transfer authorization gates)
   - See: `docs/approval-packets/program/plaid-transfer/03_verification_and_risk.md`

4. **Returns handling SOP**
   - See: `docs/approval-packets/program/plaid-transfer/04_returns_sop.md`
   - See also: `docs/operations/playbooks/transfer_returned.md`

5. **Limits policy** (per customer + per tenant)
   - See: `docs/approval-packets/program/plaid-transfer/05_limits_policy.md`

6. **Control person checklist**
   - See: `docs/approval-packets/program/plaid-transfer/06_control_person_checklist.md`

7. **Security questionnaire answers**
   - See: `docs/security/security_review_questionnaire_template.md`

8. **Evidence exports + replay bundle**
   - Receipts from Trust Spine receipt chain (redacted via DLP/Presidio)
   - Replay bundle: `docs/operations/replay_trace.md`

## Assemble Checklist
Use `docs/approval-packets/submission_checklist.md` and mark every item as complete before submission.
