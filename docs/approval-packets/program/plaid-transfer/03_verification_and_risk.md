# Verification + Risk Controls — Plaid Transfer

## Application Narrative

### KYB/KYC Steps Before Enabling Transfers
- Suite owner completes KYB (Know Your Business) verification before Plaid Transfer is enabled.
- Individual users within the suite complete KYC as required by the transfer program.
- Verification status stored in Supabase, scoped to `suite_id` + `office_id` (Law #6).
- Unverified suites cannot mint capability tokens for transfer operations (Law #3 — Fail Closed).

### Limits Policy (Initial Caps + Ramp)
- Conservative initial limits per transfer, per day, and per customer/week.
- Ramp conditions based on successful transfer history and absence of returns.
- See detailed limits: `docs/approval-packets/program/plaid-transfer/05_limits_policy.md`.
- All limit changes recorded in privileged audit log with receipts (Law #2).

### Fraud/Returns Monitoring
- ACH return webhooks processed by Finn (Money Desk) via Express Gateway.
- Return codes mapped to internal error taxonomy and risk scores.
- Escalation ladder: monitor -> reduce limits -> approval-only -> disabled.
- See: `docs/operations/playbooks/transfer_returned.md`.

### Escalation Policy
- **Elevated risk**: Set execution controls to `APPROVAL_ONLY` — all transfers require manual admin approval.
- **Suspected fraud**: Set execution controls to `DISABLED` — all transfer operations blocked immediately.
- Kill switch available per-tenant, per-provider: `docs/operations/kill_switch.md`.
- All escalation actions generate receipts (Law #2).

## Operational Rule
- Always call Plaid `authorization` endpoint before `create`.
- If authorization is declined: deny the transfer, emit a receipt with `outcome: denied`, `reason_code: authorization_declined`.
- Never proceed with transfer creation if authorization fails (Law #3 — Fail Closed).
- Ava orchestrator makes the retry/escalation decision (Law #1 — Single Brain).
