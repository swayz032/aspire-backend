# Aspire Service Level Indicators & Objectives

## Backend Orchestrator (FastAPI)

| SLI | Target SLO | Measurement |
|-----|-----------|-------------|
| Availability | 99.9% (43.8 min/month downtime) | `GET /health` success rate |
| Read latency (p50) | < 500ms | Prometheus `http_request_duration_seconds` |
| Read latency (p95) | < 2s | Prometheus `http_request_duration_seconds` |
| Action latency (p95) | < 5s | Prometheus `http_request_duration_seconds` |
| Action latency (p99) | < 10s | Prometheus `http_request_duration_seconds` |
| Error rate | < 1% of requests | `http_responses_total{status=~"5.."}` / `http_responses_total` |
| Receipt write success | 100% | `receipt_write_failures_total` = 0 |

## Desktop Server (Express)

| SLI | Target SLO | Measurement |
|-----|-----------|-------------|
| Availability | 99.9% | `GET /health` success rate |
| API proxy latency (p95) | < 3s | Circuit breaker metrics |
| Static asset latency (p95) | < 500ms | Express response time |
| Error rate | < 1% | 5xx responses / total responses |

## Admin Portal (Vite/React)

| SLI | Target SLO | Measurement |
|-----|-----------|-------------|
| Availability | 99.9% | Page load success rate |
| Page load (p95) | < 3s | Browser performance API |
| API call latency (p95) | < 2s | opsFacadeClient response time |

## External Provider SLOs

| Provider | Timeout | Circuit Breaker | Retry |
|----------|---------|----------------|-------|
| Supabase | 5s reads, 10s writes | 5 failures / 30s → open 60s | 2 retries, exponential backoff |
| Stripe | 10s | 3 failures / 60s → open 120s | 1 retry, idempotency key |
| OpenAI | 30s | 5 failures / 60s → open 120s | 2 retries, exponential backoff |
| Plaid | 10s | 3 failures / 60s → open 120s | 1 retry |
| QuickBooks | 10s | 3 failures / 60s → open 120s | 1 retry |
| PandaDoc | 10s | 3 failures / 60s → open 120s | 1 retry |
| ElevenLabs | 15s | 3 failures / 30s → open 60s | 0 retries (streaming) |
| Deepgram | 15s | 3 failures / 30s → open 60s | 0 retries (streaming) |
| LiveKit | 5s API, N/A WebRTC | 3 failures / 30s → open 60s | 1 retry |

## Burn Rate Alerting

| Window | Budget Consumption | Alert Severity |
|--------|-------------------|----------------|
| 5 min | > 14.4x burn rate | P0 — page immediately |
| 30 min | > 6x burn rate | P1 — page within 15 min |
| 6 hours | > 1x burn rate | P2 — ticket, fix within 24h |

## Error Budget Policy

- **Budget remaining > 50%**: Ship features freely
- **Budget remaining 25-50%**: Prioritize reliability work alongside features
- **Budget remaining < 25%**: Feature freeze, reliability only
- **Budget exhausted**: Incident review required before resuming feature work
