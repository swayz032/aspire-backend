# OAuth Scopes Mapping — Gusto

List required scopes and justify each. Principle: least privilege (Law #5).

## Requested Scopes

| Scope | Why Needed | Risk Tier | Mitigation |
|-------|-----------|-----------|------------|
| `companies:read` | Read company info for sync + display | GREEN | Read-only, receipts generated |
| `employees:read` | Read employee roster for payroll prep | GREEN | Read-only, PII redacted in logs (DLP/Presidio) |
| `employees:write` | Create/update employee records | YELLOW | Requires user confirmation via Ava |
| `payrolls:read` | Read payroll history + status | GREEN | Read-only, receipts generated |
| `payrolls:write` | Submit payroll runs | RED | Requires explicit authority, capability token, dual-approval for amounts >$10K |
| `webhooks:read` | Receive payroll status updates | GREEN | Signature verification enforced |

## Scope Justification
- All write scopes are gated by the Trust Spine: capability tokens (<60s), approval flows, and receipts.
- Milo (Payroll) agent only requests scopes needed for the current operation — no blanket grants.
- Scope grants are logged in the receipt chain for audit (Law #2).
- Unused scopes are not requested (principle of least privilege).
