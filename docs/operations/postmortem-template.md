# Postmortem: [Incident Title]

**Date:** YYYY-MM-DD
**Duration:** HH:MM - HH:MM (X minutes)
**Severity:** P0 / P1 / P2 / P3
**Author:** [Name]
**Status:** Draft / Final

---

## Summary

[1-2 sentences describing what happened and the impact]

## Impact

| Metric | Value |
|--------|-------|
| **Users affected** | X |
| **Suites affected** | X |
| **Duration** | X minutes |
| **Requests failed** | X |
| **Receipts lost** | 0 (append-only — should always be 0) |
| **Data leaked** | None / Describe |

## Timeline

| Time (UTC) | Event |
|------------|-------|
| HH:MM | [First indicator of problem] |
| HH:MM | [Alert triggered / User reported] |
| HH:MM | [Investigation started] |
| HH:MM | [Root cause identified] |
| HH:MM | [Mitigation applied] |
| HH:MM | [Service restored] |
| HH:MM | [Post-incident verification complete] |

## Root Cause

[Detailed technical explanation of what caused the incident]

## Receipt Evidence Trail

[Link to relevant receipts that document the incident timeline]

```
Correlation ID: <correlation_id>
Receipt chain: <receipt_ids>
```

## Aspire Laws Affected

| Law | Violated? | Details |
|-----|-----------|---------|
| Law 1: Single Brain | No/Yes | |
| Law 2: Receipt for All | No/Yes | |
| Law 3: Fail Closed | No/Yes | |
| Law 4: Risk Tiers | No/Yes | |
| Law 5: Capability Tokens | No/Yes | |
| Law 6: Tenant Isolation | No/Yes | |
| Law 7: Tools Are Hands | No/Yes | |

## Resolution

[What was done to fix the immediate problem]

## Detection

**How was the incident detected?**
- [ ] Automated alert (Prometheus/Grafana)
- [ ] Health check failure (/readyz, /healthz)
- [ ] User report
- [ ] Manual observation
- [ ] Other: ___

**Could we have detected it earlier?**
[Yes/No — if yes, what monitoring gap existed?]

## Action Items

| ID | Action | Owner | Priority | Due Date | Status |
|----|--------|-------|----------|----------|--------|
| 1 | [Fix / prevention] | [Name] | P0/P1/P2 | YYYY-MM-DD | Open |
| 2 | [Monitoring improvement] | [Name] | P1/P2 | YYYY-MM-DD | Open |
| 3 | [Test addition] | [Name] | P1/P2 | YYYY-MM-DD | Open |

## Lessons Learned

**What went well:**
- [e.g., Receipt trail made diagnosis fast]
- [e.g., Fail-closed prevented data corruption]

**What went poorly:**
- [e.g., No alert for this failure mode]
- [e.g., Runbook missing for this scenario]

**Where we got lucky:**
- [e.g., Low traffic period minimized impact]

## Production Gate Review

Does this incident require updates to any Production Gate?

- [ ] Gate 1 (Testing): New test needed?
- [ ] Gate 2 (Observability): New metric/alert needed?
- [ ] Gate 3 (Reliability): Circuit breaker/timeout change?
- [ ] Gate 4 (Operations): Runbook update needed?
- [ ] Gate 5 (Security): Security review needed?
