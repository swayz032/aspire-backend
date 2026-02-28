# Webhook Mapping — Gusto

List webhook event types consumed by Aspire and how they map to internal events/receipts.

## Event Mapping

| Gusto Event | Meaning | Aspire Agent | Receipt Type | State Updates |
|-------------|---------|-------------|-------------|---------------|
| `payroll.submitted` | Payroll run submitted | Milo (Payroll) | `payroll.status_change` | Payroll state -> submitted |
| `payroll.processed` | Payroll successfully processed | Milo (Payroll) | `payroll.status_change` | Payroll state -> processed |
| `payroll.failed` | Payroll processing failed | Milo (Payroll) | `payroll.status_change` | Payroll state -> failed, trigger playbook |
| `employee.created` | New employee added | Milo (Payroll) | `employee.sync` | Employee roster updated |
| `employee.updated` | Employee details changed | Milo (Payroll) | `employee.sync` | Employee record updated |
| `employee.terminated` | Employee terminated | Milo (Payroll) | `employee.sync` | Employee status -> terminated |
| `company.updated` | Company details changed | Milo (Payroll) | `company.sync` | Company record updated |

## Webhook Processing Rules
- All webhooks verified via HMAC-SHA256 signature (`GUSTO_WEBHOOK_SECRET`).
- Idempotent processing — duplicate events acknowledged but not re-processed.
- Every webhook generates a receipt in the Trust Spine (Law #2).
- Failed webhooks (signature mismatch) generate a security receipt and are rejected (Law #3).
- Webhook payloads are transient — processed and discarded, not stored raw.
- PII in webhook data is redacted via DLP/Presidio before any logging (Gate 5).
