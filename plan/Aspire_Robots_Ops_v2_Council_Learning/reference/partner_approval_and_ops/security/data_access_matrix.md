# Data Access Matrix (Template)

| Data | Storage | Access | Purpose | Retention |
|---|---|---|---|---|
| OAuth tokens | secrets store | system only | API calls | until revoked |
| provider_call_log (redacted) | DB | tenant admins/ops | debugging | 30-90 days |
| receipts | DB | tenant roles | audit | immutable |
| approvals | DB | tenant roles | governance | 1-7 years |
