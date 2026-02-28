# Pilot Plan — Gusto Embedded Payroll

## Phase 1: Internal Beta
- **Cohort**: 3-5 internal test suites (Aspire team accounts).
- **Mode**: `APPROVAL_ONLY` — every payroll operation requires manual admin approval.
- **Duration**: 2 weeks minimum.
- **Monitoring**: Full receipt chain analysis, Grafana dashboards for error rate + latency.

## Phase 2: Limited External Beta
- **Cohort**: 5-10 selected customers (small businesses, <20 employees each).
- **Mode**: `APPROVAL_ONLY` for RED tier (payroll runs), normal for GREEN/YELLOW.
- **Duration**: 4 weeks.
- **Limits**: Conservative payroll caps per the Milo skill pack policy.
- **Support**: Dedicated playbooks (`docs/operations/playbooks/payroll_run_failed.md`).

## Phase 3: Graduated Rollout
- **Cohort**: All customers with Gusto connection.
- **Mode**: Normal operation — RED tier still requires explicit authority (always).
- **Ramp**: Gradual increase in customer count, monitored weekly.

## Monitoring + Rollback Plan
- **Metrics**: Payroll success rate, error rate, latency p95, receipt coverage (must be 100%).
- **Alerts**: P0 on any payroll execution failure, P1 on elevated error rate.
- **Rollback**: Flip all suites to `APPROVAL_ONLY` via Admin API kill switch. Receipt generated for the change.
- **Kill switch**: `docs/operations/kill_switch.md` — immediate mitigation for any suspected issue.

## Exit Criteria (Move to Next Phase)
- [ ] Zero payroll execution errors in pilot cohort for 2+ weeks
- [ ] 100% receipt coverage verified by receipt-ledger-auditor
- [ ] All support playbooks exercised at least once (even in simulation)
- [ ] Customer feedback collected and addressed
