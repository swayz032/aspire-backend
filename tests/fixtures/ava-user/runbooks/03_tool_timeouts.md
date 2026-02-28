# Runbook: Tool Timeout / Rate Limits

**Source:** Ava User Enterprise Handoff v1.1

## Detection
- Elevated tool error rate
- Increased latency

## Mitigation
- Retries with backoff for safe idempotent calls only.
- Circuit breaker per tool rail.
- Degrade to draft-only with user notification.
