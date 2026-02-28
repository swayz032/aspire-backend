---
gate: 9
name: "SLO Dashboard"
status: "complete"
phase_introduced: "4"
complexity: "low"
critical: false
---

# GATE 09: SLO Dashboard

## Requirement

Live p95 latency, error rates, retry budgets, uptime tracking - operational visibility.

## Service Level Objectives (SLOs)

### SLO 1: API Latency
- **Target:** p95 latency <800ms
- **Measurement:** 95th percentile response time for all API endpoints
- **Error Budget:** 5% of requests can exceed 800ms (20 failures per 400 requests)

### SLO 2: Tool Success Rate
- **Target:** 99% success rate (1% error budget)
- **Measurement:** Ratio of successful tool calls to total tool calls
- **Example:** Stripe API, Gmail API, Calendar API calls

### SLO 3: Receipt Generation Rate
- **Target:** 100% coverage (zero error budget)
- **Measurement:** Every action MUST generate receipt
- **Critical:** No actions bypass receipt generation

### SLO 4: Uptime
- **Target:** 99.5% uptime (43.8 hours downtime/year allowed)
- **Measurement:** Percentage of time system is responsive
- **Excludes:** Planned maintenance windows

### SLO 5: Cold Start Performance
- **Target:** <2.5s (mobile app launch to Ava ready)
- **Measurement:** Time from app open to first interaction
- **Platform:** iOS + Android

## Dashboard Components

### 1. Real-Time Metrics
- **p50/p95/p99 Latency:** Current percentiles across all endpoints
- **Error Rate:** Rolling 1-hour error percentage
- **Active Requests:** Current in-flight requests
- **Queue Depth:** Pending background jobs

### 2. Error Budget Tracking
```
┌─────────────────────────────────────────┐
│ Error Budget Status (30-day window)     │
├─────────────────────────────────────────┤
│ API Latency (<800ms p95)                │
│ Budget: 5% | Used: 2.3% | Remaining: 57%│
│ [████████████████░░░░] HEALTHY          │
│                                          │
│ Tool Success Rate (99%)                 │
│ Budget: 1% | Used: 0.4% | Remaining: 60%│
│ [█████████████████████] HEALTHY         │
│                                          │
│ Receipt Coverage (100%)                 │
│ Budget: 0% | Used: 0% | Remaining: 100% │
│ [████████████████████] PERFECT          │
└─────────────────────────────────────────┘
```

### 3. Retry Budget Monitor
- **Stripe API:** Retries allowed: 3, Current retry rate: 0.2%
- **Gmail API:** Retries allowed: 3, Current retry rate: 0.1%
- **Calendar API:** Retries allowed: 3, Current retry rate: 0.05%

### 4. Alerting Thresholds
- **Warning:** Error budget >50% consumed
- **Critical:** Error budget >90% consumed
- **Page Ops:** SLO breach (error budget exhausted)

## Verification Criteria

- [ ] SLO dashboard accessible (internal tooling)
- [ ] Live metrics updating (p50/p95/p99, error rate, uptime)
- [ ] Error budget tracking functional (30-day rolling window)
- [ ] Alerts configured for SLO breaches
- [ ] Historical data retained (90 days minimum)

## What This Gate Prevents

- **Blind operations** - No visibility into system health until users complain
- **SLO drift** - Latency/errors creeping up without detection
- **Surprise outages** - No early warning before critical failure

## Failure Scenarios

❌ **Fails if:**
- SLO dashboard missing or non-functional
- Metrics not updating in real-time (stale data)
- No alerting on SLO breaches
- Cannot view historical trends

✅ **Passes if:**
- SLO dashboard live and accessible
- Real-time metrics visible (p95, error rate, uptime)
- Error budget tracking working
- Alerts functional

## Implementation

### Monitoring Stack
**Option 1: Sentry (Recommended)**
- Error tracking + performance monitoring
- Built-in SLO dashboard
- Automatic alerting
- Correlation with receipts (trace IDs)

**Option 2: PostHog + Custom Dashboard**
- Event-based analytics
- Custom SLO dashboard (Grafana/Metabase)
- Self-hosted option available

### SLO Calculation Example
```python
# Calculate p95 latency SLO compliance
def check_latency_slo(metrics, threshold_ms=800):
    """
    Check if p95 latency SLO is met.

    Args:
        metrics: List of request latencies (ms)
        threshold_ms: SLO threshold (default 800ms)

    Returns:
        dict: SLO status (met, error_budget_used)
    """
    p95_latency = np.percentile(metrics, 95)

    # Count violations (requests exceeding threshold)
    violations = sum(1 for m in metrics if m > threshold_ms)
    error_budget_used = (violations / len(metrics)) * 100

    return {
        "p95_latency": p95_latency,
        "threshold": threshold_ms,
        "slo_met": p95_latency < threshold_ms,
        "error_budget_used": error_budget_used,
        "error_budget_remaining": 5.0 - error_budget_used  # 5% budget
    }
```

## SLO Review Cadence

### Daily
- Check error budget consumption
- Review any critical alerts
- Investigate anomalies (spikes, unusual patterns)

### Weekly
- SLO compliance review (all 5 SLOs)
- Error budget trending (increasing/decreasing?)
- Action items for SLO violations

### Monthly
- SLO retrospective (what caused breaches?)
- Adjust SLOs if needed (too strict? too lenient?)
- Document learnings and improvements

## Related Gates

- **Gate 08:** Replay Demo (correlation IDs link metrics to receipts)
- **Gate 10:** Incident Runbooks (SLO breaches trigger runbooks)

## Status: ✅ COMPLETE

**Verification Date:** 2026-01-10
**Verified By:** Phase 4 roadmap includes SLO dashboard requirement
**Evidence:** SLO dashboard spec in Aspire-Production-Roadmap.md:2140-2153 (Gate 8: Performance Budgets) and Gate 10 operational requirements
