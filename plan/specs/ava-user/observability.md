# Observability Specification

**Source:** Ava User Enterprise Handoff v1.1

## Correlation
- Propagate `correlation_id` through:
  - orchestrator logs
  - policy engine logs
  - worker logs
  - receipts

## Metrics (minimum)
- `ava_orchestrator_requests_total{status}`
- `ava_policy_denials_total{reason}`
- `ava_approvals_total{decision}`
- `ava_tool_invocations_total{tool,action,status}`
- `ava_receipt_writes_total{type,status}`
- Latency histograms: p50/p95/p99 for orchestrator and tool calls

## Logging
- Structured JSON logs
- PII redaction in logs by default

## Governance Checklist
- 100% schema validation
- 0 unreceipted side effects
- Approval binding to payload hash
- Capability token expiry enforced
- Tenant isolation verified

## Cross-reference
- SLI/SLO definitions: `infrastructure/observability/SLI_SLO.md`
- DLP redaction matrix: `plan/specs/ava-admin/dlp_redaction_matrix.md`
- Gate 02 (Observability): correlation IDs, SLO dashboards, health checks
