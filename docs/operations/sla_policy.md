# SLA / Support Policy (Starter)

## Support Hours
- **Initial**: Business hours (US Eastern, 9am-6pm Mon-Fri) + emergency escalation for critical incidents (P0).
- **P0 incidents**: 24/7 response via on-call rotation (see `docs/operations/on_call_minimal.md`).

## Response Targets (Starter)

| Priority | Definition | Acknowledge | Resolve | Examples |
|----------|-----------|------------|---------|---------|
| P0 | Payroll/transfer execution broken, security incident, data breach | Within 1 hour | Within 4 hours (mitigate), 24 hours (resolve) | Failed payroll near pay date, unauthorized transfer, cross-tenant leak |
| P1 | Provider outage, webhook backlog, elevated error rate | Within 4 hours | Within 24 hours | Gusto API down, Plaid webhooks delayed, receipt chain degraded |
| P2 | User-impacting bug with workaround available | Within 1 business day | Within 1 week | Single skill pack degraded, UI rendering issue, non-critical sync delay |

## Escalation

### P0 Escalation Path
1. Flip execution controls to `APPROVAL_ONLY` or `DISABLED` via kill switch (immediate).
2. Notify impacted tenants via Ava (Eli inbox / Sarah front desk).
3. Start incident channel and engage on-call engineer.
4. Follow incident response runbook: `docs/operations/incident_response.md`.

### P1 Escalation Path
1. Assess impact scope (which suites/agents affected).
2. If provider-side: check provider status page, wait or reconcile.
3. If Aspire-side: engage engineering for investigation.
4. Notify affected tenants if impact >1 hour.

## Evidence Requirements
- Keep receipts + replay bundle for all P0/P1 incidents.
- Evidence must be preserved before any remediation (receipts are immutable — Law #2).
- Postmortem required within 48 hours for all P0 incidents, 1 week for P1.

## SLO Targets (Internal)

| Metric | Target | Measurement |
|--------|--------|-------------|
| Orchestrator availability | 99.9% uptime | Health check probes (/healthz, /readyz) |
| Receipt coverage | 100% | Receipt-ledger-auditor verification |
| Tool execution latency (p95) | <5 seconds | Prometheus metrics |
| Orchestrator latency (p95) | <30 seconds | Prometheus metrics |
| Webhook processing latency (p95) | <10 seconds | Express Gateway metrics |
