# Embedded Payroll Demo Script — Gusto + Aspire

## Demo Flow

### 1. OAuth Connect
- Suite owner initiates Gusto connection through Aspire UI.
- Ava orchestrator guides the OAuth flow — consent screen shows requested scopes.
- Token stored encrypted in Supabase, scoped to `suite_id` + `office_id` (Law #6).
- Receipt generated: `action_type: provider.connect`, `outcome: success`.

### 2. Read-Only Sync (GREEN tier)
- Milo (Payroll) reads company info + employee roster from Gusto.
- GREEN tier — auto-approved, no user confirmation needed (Law #4).
- Capability token minted (<60s), scoped to `gusto.employees.read`.
- Receipt generated for the sync operation.

### 3. Draft Payroll (No Execution)
- Milo prepares a payroll draft based on employee hours/salary data.
- Draft is presented to the user for review — no execution yet.
- YELLOW tier — user sees the draft and confirms accuracy.

### 4. Approval Gate (RED tier)
- User approves the payroll run through Ava's authority UX (Hot interaction state — video/voice presence for binding decisions).
- Approval evidence recorded: who approved, when, what amount, what employees.
- Dual-approval required for payroll amounts >$10K (same-approver bypass blocked).

### 5. Capability Token Issued + Consumed
- Ava orchestrator mints a capability token: `tool: gusto.payroll.submit`, `suite_id`, `office_id`, expires in <60s.
- Token is single-use and server-verified (Law #5).
- Milo consumes the token during execution.

### 6. Execute via LangGraph Orchestrator (Idempotent)
- Milo submits payroll to Gusto API using the capability token.
- Idempotency key prevents duplicate submission (Gate 3).
- Execution flows through the LangGraph orchestrator (Law #1 — Single Brain).

### 7. Receipts + Redacted Provider Logs
- Receipt generated: `action_type: payroll.run`, `outcome: success`, `risk_tier: red`.
- Provider call log stored with PII redacted via DLP/Presidio (Gate 5).
- Receipt includes: `correlation_id`, `trace_id`, `approval_evidence`, `capability_token_id`.

### 8. Kill Switch Demo (APPROVAL_ONLY / DISABLED)
- Demonstrate switching execution controls to `APPROVAL_ONLY`:
  - All payroll operations require manual admin approval (even previously auto-approved reads).
  - Receipt generated for the mode change.
- Demonstrate switching to `DISABLED`:
  - All Gusto operations blocked. Fail closed (Law #3).
  - Receipt generated with `outcome: denied`, `reason_code: provider_disabled`.
- Restore to `ENABLED` and verify normal operation resumes.
