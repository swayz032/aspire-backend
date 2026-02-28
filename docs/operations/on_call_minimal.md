# Minimal On-Call Plan (Solo-Founder Friendly)

## Alert Channels
- **Primary**: Email + SMS (or Slack) to on-call engineer.
- **Escalation**: Phone call if no acknowledgment within 15 minutes (P0 only).
- **Dashboard**: Grafana (`:3000`) for real-time monitoring.

## Severity Levels

| Severity | Definition | Immediate Action |
|----------|-----------|-----------------|
| SEV0 | Money movement / payroll execution wrong, security breach | Flip execution controls to `DISABLED`. Engage incident response immediately. |
| SEV1 | Persistent provider outage / webhook backlog (>1 hour) | Flip to `APPROVAL_ONLY` for affected provider. Monitor and reconcile. |
| SEV2 | Partial degradation (single skill pack, non-financial) | Monitor. Notify affected tenants if impact persists >4 hours. |

## Immediate Mitigation
- For any SEV0/SEV1: flip `execution_controls` to `APPROVAL_ONLY` or `DISABLED` via Admin API kill switch.
- Kill switch procedure: `docs/operations/kill_switch.md`.
- Receipt auto-generated for every mode change (Law #2).

## On-Call Responsibilities
1. Acknowledge alert within response target (see `docs/operations/sla_policy.md`).
2. Assess severity and impact scope.
3. Mitigate immediately (kill switch if needed).
4. Investigate root cause.
5. Communicate status to affected tenants.
6. Resolve or escalate.
7. Write postmortem (P0: within 48h, P1: within 1 week).

## Solo-Founder Phase Notes
- Single on-call engineer (founder) during business hours.
- After-hours: SEV0 alerts trigger phone call; SEV1/SEV2 wait until next business day unless financially impactful.
- As team grows: implement rotation schedule (weekly rotation recommended).
