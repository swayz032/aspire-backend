# Incident Response Runbook

## Severity Levels

| Severity | Definition | Response Time | Examples |
|----------|-----------|--------------|---------|
| P0 | Money movement / payroll execution wrong, security breach, data leak | Acknowledge <1 hour | Failed payroll near pay date, unauthorized transfer, cross-tenant data leak |
| P1 | Persistent provider outage, webhook backlog, elevated error rate | Acknowledge <4 hours | Gusto API down >1h, Plaid webhooks delayed, receipt chain broken |
| P2 | Partial degradation with workaround | Acknowledge <1 business day | Single skill pack degraded, non-critical UI issue |

## Incident Response Steps

### 1. Mitigate
- **Immediate action**: Set `execution_controls` to `APPROVAL_ONLY` or `DISABLED` for the affected provider/tenant.
- Use Admin API: `POST /admin/kill-switch` with `{ provider, suite_id, mode }`.
- For P0 money/payroll incidents, default to `DISABLED` (Law #3 — Fail Closed).
- Receipt auto-generated for the mode change (Law #2).
- See: `docs/operations/kill_switch.md` for detailed kill switch procedure.

### 2. Identify Trace IDs
- Query the Trust Spine receipt chain for the affected time window.
- Collect `trace_id` and `correlation_id` values for all related operations.
- Use Grafana dashboards (`:3000`) to correlate across services.
- Correlation IDs flow through: Express Gateway -> LangGraph Orchestrator -> Tool Executor -> Receipt Chain.

### 3. Export Evidence
- Export receipts for the affected trace IDs (redacted via DLP/Presidio).
- Export provider call logs (redacted).
- Preserve all evidence before any remediation — receipts are immutable (Law #2).

### 4. Replay in SIMULATE Mode
- Use the replay harness to reproduce the incident in simulation mode.
- Compare actual vs expected outputs.
- Create a failing test case if the replay reveals a mismatch.
- See: `docs/operations/replay_trace.md` for replay procedure.

### 5. Fix + Add Regression Tests
- Implement the fix with appropriate test coverage.
- Add regression test to prevent recurrence (Gate 1).
- Add evil test if the incident was security-related (Gate 5).
- All fixes flow through normal PR review process.

### 6. Postmortem + Prevention
- Write postmortem within 48 hours of incident resolution.
- Use template: `docs/operations/status_page_template.md` (postmortem section).
- Include: root cause, timeline, what went well, what went wrong, action items with owners + due dates.
- Store postmortem in `docs/operations/postmortems/` directory.
- Update relevant playbooks with lessons learned.
