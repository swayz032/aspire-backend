# Aspire SLI/SLO Definitions — Phase 0B Baseline

## Service Level Indicators (SLIs)

| SLI | Measurement | Data Source |
|-----|-------------|-------------|
| **Outbox Success Rate** | `successful_outbox_deliveries / total_outbox_attempts` | Prometheus: `aspire_outbox_*` metrics via OTEL |
| **Outbox Latency (p95)** | Time from outbox enqueue to delivery confirmation | Prometheus: `aspire_outbox_latency_seconds` histogram |
| **Approval Latency (p95)** | Time from approval request to user decision | Prometheus: `aspire_approval_latency_seconds` histogram |
| **Receipt Write Availability** | `successful_receipt_writes / total_receipt_write_attempts` | Prometheus: `aspire_receipt_write_*` metrics |
| **Tool Execution Success Rate** | `successful_tool_calls / total_tool_calls` per tool type | Prometheus: `aspire_tool_execution_*` metrics |

## Service Level Objectives (SLOs)

| SLO | Target | Window | Alert Threshold |
|-----|--------|--------|-----------------|
| **Outbox Success Rate** | >= 99% | 24h rolling | < 97% triggers page |
| **Outbox Latency (p95)** | <= 2 min | 24h rolling | > 5 min triggers ticket |
| **Approval Latency (p95)** | <= 30 min | 24h rolling | > 15 min triggers ticket |
| **Receipt Write Availability** | >= 99.9% | 24h rolling | < 99.5% triggers page |
| **Tool Execution Success Rate** | >= 95% per tool | 24h rolling | < 90% triggers ticket |

## Error Budgets

- **Outbox:** 1% failure budget = ~14 min downtime / 24h
- **Receipt Writes:** 0.1% failure budget = ~1.4 min downtime / 24h
- **Tool Execution:** 5% failure budget per tool type (higher tolerance for external provider failures)

## Alert Rules (Prometheus)

See `infrastructure/docker/otel/alert_rules.yml` for implemented alerts:
- `OutboxQueueStuck` — oldest pending > 5 min for 5 min → severity: page
- `ApprovalLatencyHigh` — p95 > 15 min for 15 min → severity: ticket

## Production Gate Mapping

- **Gate 2 (Observability):** SLO dashboards defined (this doc), correlation IDs flowing (Phase 1), health checks (Phase 1)
- **Gate 4 (Operations):** Alert rules loaded, runbook references in alert labels

## Future Expansion (Phase 1+)

- Add per-agent SLIs (Ava orchestration latency, tool-specific error rates)
- Add Sentry error rate SLI
- Add LangGraph decision latency SLI
- Add capability token validation latency SLI
