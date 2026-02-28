# Data Access Matrix

## Access Control Overview
All data access is governed by Row-Level Security (Law #6) and the principle of least privilege (Law #5).

## Data Classification

| Data | Storage | Access | Purpose | Retention | RLS Enforced |
|------|---------|--------|---------|-----------|--------------|
| OAuth tokens | Supabase (encrypted) | System only (orchestrator + tool executor) | Provider API calls | Until revoked | Yes (suite_id) |
| Capability tokens | Orchestrator state (in-memory) | System only (orchestrator) | Tool execution authorization | <60s (auto-expire) | N/A (in-memory) |
| Provider call log (redacted) | Supabase | Tenant admins + ops | Debugging, audit | 30-90 days | Yes (suite_id) |
| Receipts | Supabase + in-memory store | Tenant roles (via RLS) | Audit trail, compliance | Immutable, never deleted | Yes (suite_id) |
| Approvals | Supabase | Tenant roles (via RLS) | Governance evidence | 1-7 years (regulatory) | Yes (suite_id) |
| User PII | Supabase | Tenant members (via RLS) | Account management | Until account deletion | Yes (suite_id + office_id) |
| Financial data | Supabase | Tenant admins (via RLS) | Invoicing, payments, payroll | Per regulatory requirement | Yes (suite_id + office_id) |
| Webhook payloads | Express Gateway (transient) | System only | Event processing | Not persisted (processed + discarded) | N/A (transient) |
| Metrics | Prometheus + Grafana | Internal/localhost only | Observability | 30 days (Prometheus) | N/A (no tenant data) |
| Logs | Application logs | Ops team | Debugging | 30 days | N/A (PII redacted via DLP) |

## Access Roles

| Role | Receipts | Provider Logs | Financial Data | User PII | OAuth Tokens | Admin API |
|------|----------|--------------|---------------|----------|-------------|-----------|
| Suite Owner | Read (own suite) | Read (own suite, redacted) | Read/Write (own suite) | Read/Write (own suite) | Never | Full (own suite) |
| Office Admin | Read (own office) | Read (own office, redacted) | Read/Write (own office) | Read/Write (own office) | Never | Limited (own office) |
| Office Member | Read (own office) | No | Read (own office, limited) | Read (own, limited) | Never | No |
| Ava (Orchestrator) | Read/Write (all, scoped by request) | Read/Write (scoped) | Read/Write (scoped by token) | Read (scoped by token) | Read (scoped by token) | N/A |
| Ops (Internal) | Read (cross-tenant, redacted) | Read (cross-tenant, redacted) | No | No | No | Metrics + health only |

## Enforcement Points
- **Database layer**: Supabase RLS policies enforce `suite_id` scoping on every query.
- **API layer**: Express Gateway validates JWT + extracts `suite_id`/`office_id` from auth context (not client payload).
- **Orchestrator layer**: Capability tokens scope tool execution to specific tenant + action.
- **Logging layer**: DLP (Presidio) redacts PII before any log output (Gate 5).
