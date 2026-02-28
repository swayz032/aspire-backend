# n8n Hardening Audit — Phase 0B

## Law Compliance Matrix

| Aspire Law | n8n Enforcement | Status |
|------------|----------------|--------|
| **Law #1: Single Brain** | All workflows are request-triggered (webhook/cron). No autonomous decision-making. n8n is plumbing, not brain. | ENFORCED |
| **Law #2: Receipt for All** | Workflow outputs must include `receipt_id` or `correlation_id` for traceability. (Phase 2: wire to Trust Spine) | DEFERRED (N8N-005) |
| **Law #3: Fail Closed** | Workflow error handling: all errors produce structured error output. No silent failures. No fallback execution. | ENFORCED |
| **Law #7: Tools Are Hands** | Workflows do NOT make decisions. They execute bounded operations and return results to the orchestrator. | ENFORCED |

## Security Configuration

| Control | Setting | Verified |
|---------|---------|----------|
| Basic Auth | Enabled (`N8N_BASIC_AUTH_ACTIVE=true`) | YES |
| Auth Credentials | Externalized via env vars (not hardcoded) | YES |
| API Key | Generated and scoped to local dev | PENDING (runtime) |
| Diagnostics | Disabled (`N8N_DIAGNOSTICS_ENABLED=false`) | YES |
| Personalization | Disabled (`N8N_PERSONALIZATION_ENABLED=false`) | YES |
| Network | localhost only (port 5678, not exposed externally) | YES |
| Database | Isolated Postgres instance (n8n-db on port 5433, separate from dev DB) | YES |

## Workflow Hardening Checklist

For every n8n workflow deployed in Aspire:

- [ ] **Trigger type:** Webhook or Cron only (no autonomous triggers)
- [ ] **Idempotency:** Workflow handles duplicate executions gracefully (idempotency key check)
- [ ] **Error handling:** All error paths produce structured output (no silent failures)
- [ ] **Correlation ID:** Every workflow execution carries a `correlation_id` for receipt tracing
- [ ] **Timeout:** Maximum execution time configured (prevent runaway workflows)
- [ ] **No decisions:** Workflow does not make autonomous choices — returns data to orchestrator
- [ ] **No retries:** Workflow does not retry failed operations (orchestrator decides retry policy)
- [ ] **Secrets:** No hardcoded credentials in workflow definitions (use n8n credentials store)
- [ ] **Logging:** Structured log output, no PII in logs

## Workflow Templates (Scaffold State)

| Workflow | Trigger | Purpose | Full Implementation |
|----------|---------|---------|---------------------|
| `FIN_DAILY_SYNC` | Cron (daily) | Financial data sync trigger | Phase 2 |
| `MAIL_DELIVERABILITY_MONITOR` | Cron (hourly) | Email deliverability health check | Phase 0C |
| `MAIL_DNS_CHECK_SCHEDULE` | Cron (6h) | DNS record validation | Phase 0C |
| `MAIL_IMAP_SYNC_SCHEDULE` | Cron (15min) | IMAP mailbox sync trigger | Phase 0C |
| `MAIL_INCIDENT_ESCALATION` | Webhook | Mail incident escalation trigger | Phase 0C |

## Deferred Items

- **N8N-005: Wire n8n to Trust Spine** — Requires LangGraph orchestrator (Phase 1B). n8n workflows will emit receipts via the orchestrator's receipt pipeline, not directly to Supabase.
- **Full workflow implementations** — Current templates are scaffolds with correct trigger patterns. Business logic implemented in Phases 0C (mail) and 2 (finance).
