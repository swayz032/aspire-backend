# Aspire Backend Orchestrator — Incident Runbook

## Service Overview

| Property | Value |
|----------|-------|
| Service | aspire-orchestrator |
| Port | 8000 (WSL2 local), Railway production |
| Framework | FastAPI + LangGraph |
| Runtime | Python 3.11, venv at `~/venvs/aspire` |
| Health endpoints | GET /healthz (liveness), GET /livez, GET /readyz (readiness) |
| Metrics | GET /metrics (Prometheus) |
| Logs | stdout (uvicorn), structured JSON with correlation_id |
| Deployment | Railway (`swayz032/aspire-backend`) |
| Production URL | https://www.aspireos.app |

## Dependency Map

| Dependency | Required? | Impact if Down |
|------------|-----------|----------------|
| ASPIRE_TOKEN_SIGNING_KEY | Yes | All token mints fail — CAPABILITY_TOKEN_REQUIRED on every call |
| DLP/Presidio | Yes | /readyz returns 503; receipts may contain unredacted PII (Law #9 risk) |
| Supabase pooler (aws-1-us-east-1.pooler.supabase.com:6543) | Yes (prod) | Receipt persistence fails; in-memory fallback activates |
| Redis (port 6379) | No (Phase 1) | Outbox queue not processed; in-memory state only |
| OpenAI API | Yes (LLM ops) | All brain operations fail; Ava responses blocked |
| Safety gateway (port 8787) | Yes (prod) | NeMo Guardrails offline; safety gate falls through |
| n8n (port 5678) | No | Workflow triggers degrade; approval webhooks undelivered |

## Quick Diagnosis Commands

```bash
# Is the process alive?
curl -s http://localhost:8000/healthz | jq .

# Are all dependencies ready?
curl -s http://localhost:8000/readyz | jq .

# What is the error rate right now?
curl -s http://localhost:8000/metrics | grep aspire_orchestrator_requests_total

# Check receipt write failure counter
curl -s http://localhost:8000/metrics | grep receipt_write_failures_total

# WSL2: Check process is running
wsl -d Ubuntu-22.04 -e bash -c "ps aux | grep uvicorn"

# Railway production logs
railway logs --tail 100
```

---

## Failure Mode 1: Orchestrator Unhealthy (FastAPI on Port 8000)

### Symptoms
- `curl http://localhost:8000/healthz` times out or returns non-200
- All client requests return 503 or connection refused
- Prometheus alert `OrchestratorDown` fires

### Diagnosis

```bash
# 1. Check if process is running at all
wsl -d Ubuntu-22.04 -e bash -c "ps aux | grep uvicorn | grep -v grep"

# 2. Check what is bound on port 8000
wsl -d Ubuntu-22.04 -e bash -c "ss -tlnp | grep :8000"

# 3. Check last logs for crash reason
wsl -d Ubuntu-22.04 -e bash -c "journalctl -u aspire-orchestrator --since '10 minutes ago' 2>/dev/null || echo 'no systemd unit'"

# 4. Check Docker if running containerized
docker logs aspire-orchestrator --tail 50

# 5. Railway production
railway logs --service aspire-backend --tail 100
```

### Resolution

```bash
# Option A: Restart in WSL2
wsl -d Ubuntu-22.04 -e bash -c "
  source ~/venvs/aspire/bin/activate
  pkill -f 'uvicorn.*aspire_orchestrator' || true
  sleep 2
  cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator
  python -m uvicorn aspire_orchestrator.server:app --host 0.0.0.0 --port 8000
"

# Option B: Railway redeploy (production)
railway redeploy --service aspire-backend

# Verify recovery
curl -s http://localhost:8000/healthz | jq .status
curl -s http://localhost:8000/readyz | jq .status
```

### Escalation
- P0 if Railway production: Page on-call immediately. Kill switch to DISABLED for all operations.
- P2 if local dev only: Restart and investigate logs before resuming work.

---

## Failure Mode 2: Database Connection Failures (Supabase Pooler)

### Symptoms
- `/readyz` shows `"supabase_connected": false`
- Receipt writes failing — `receipt_write_failures_total` counter increasing
- `RECEIPT_WRITE_FAILED` errors in logs
- Prometheus alert `ReceiptWriteFailures` fires (Law #2 violation risk)

### Diagnosis

```bash
# 1. Check readyz for Supabase status
curl -s http://localhost:8000/readyz | jq '.checks.supabase_connected'

# 2. Check Prometheus for receipt failures
curl -s http://localhost:8000/metrics | grep receipt_write_failures_total

# 3. Test Supabase pooler connectivity directly
# Pooler: aws-1-us-east-1.pooler.supabase.com:6543
# NOTE: Direct host is IPv6 only — use pooler, not direct
wsl -d Ubuntu-22.04 -e bash -c "
  nc -zv aws-1-us-east-1.pooler.supabase.com 6543
"

# 4. Check Supabase status page
# https://status.supabase.com

# 5. Review env variable
wsl -d Ubuntu-22.04 -e bash -c "echo \$ASPIRE_SUPABASE_URL | cut -c1-30"
```

### Resolution

```bash
# 1. Verify env vars are set correctly
# Connection string format: postgresql://postgres.qtuehjqlcmfcascqjjhc:<PASSWORD>@aws-1-us-east-1.pooler.supabase.com:6543/postgres
# NOTE: ! in password must be URL-encoded as %21

# 2. If transient (Supabase provider incident): Wait for recovery, monitor Supabase status
# Receipts will queue in-memory; validate chain integrity post-recovery

# 3. If credentials expired: Rotate via AWS Secrets Manager
aws secretsmanager get-secret-value --secret-id aspire/dev/supabase --region us-east-1

# 4. Force orchestrator restart after credential fix
pkill -f "uvicorn.*aspire_orchestrator" || true
python -m uvicorn aspire_orchestrator.server:app --host 0.0.0.0 --port 8000
```

### Post-Recovery
After Supabase reconnects, verify receipt chain integrity:
```bash
curl -X POST http://localhost:8000/v1/receipts/verify-run \
  -H "Content-Type: application/json" \
  -d '{"suite_id": "<affected-suite-id>"}'
```

### Escalation
- P1 if production: Receipt writes are failing — this is a Law #2 risk. Activate kill switch (`APPROVAL_ONLY`) for all YELLOW/RED operations until Supabase is confirmed healthy.

---

## Failure Mode 3: Redis Unavailable

### Symptoms
- Outbox queue not processing
- `QUEUE_BACKEND_UNAVAILABLE` in logs
- `/admin/ops/outbox` shows stuck jobs
- `outbox_queue_depth` growing without draining

### Diagnosis

```bash
# 1. Check Redis is running (WSL2, port 6379)
wsl -d Ubuntu-22.04 -e bash -c "redis-cli ping"

# 2. Check outbox status via admin API
curl -s http://localhost:8000/admin/ops/outbox | jq .

# 3. Check readiness contract for queue backend
curl -s http://localhost:8000/admin/ops/readiness-contract | jq '.outbox_backend'

# 4. Check Prometheus for queue depth
curl -s http://localhost:8000/metrics | grep outbox_queue_depth
```

### Resolution

```bash
# Option A: Restart Redis (WSL2)
wsl -d Ubuntu-22.04 -e bash -c "sudo systemctl restart redis-server"
wsl -d Ubuntu-22.04 -e bash -c "redis-cli ping"  # Should return PONG

# Option B: If Redis data is corrupted, flush and restart
# WARNING: This discards all in-flight queue jobs
wsl -d Ubuntu-22.04 -e bash -c "redis-cli FLUSHDB"

# Option C: Phase 1 fallback — orchestrator uses in-memory queue automatically
# No action required — degrade is graceful
```

### Escalation
- P2: Outbox queue not critical in Phase 1 (in-memory fallback). Upgrade to P1 if production deployment uses Redis as primary outbox backend.
- Monitor queue depth after Redis recovery — large backlogs may cause thundering herd.

---

## Failure Mode 4: OpenAI API Failures / Rate Limiting

### Symptoms
- `OPENAI_API_ERROR` or `RATE_LIMIT_EXCEEDED` in logs
- All LLM-dependent operations (Ava responses, agent reasoning) failing
- `SAFETY_BLOCKED` errors if NeMo Guardrails cannot reach model
- Prometheus `HighErrorRate` alert may fire

### Diagnosis

```bash
# 1. Check provider call logs via admin API
curl -s "http://localhost:8000/admin/ops/provider-calls?provider=openai&status=failed" \
  -H "X-Admin-Token: <token>" | jq .

# 2. Check OpenAI status
# https://status.openai.com

# 3. Check Ollama (used for NeMo safety gate only — llama3:8b)
curl http://localhost:11434/api/tags | jq '.models[].name'

# 4. Check API key in secrets
aws secretsmanager get-secret-value --secret-id aspire/dev/openai --region us-east-1 | jq .SecretString
```

### Resolution

```bash
# 1. Rate limiting (429): Wait for rate limit window reset (typically 1 min)
# Circuit breaker (SLO doc): 5 failures / 60s window -> open for 120s
# Wait for circuit breaker half-open (120s), then test a single probe request

# 2. API key expired or invalid: Rotate via AWS Secrets Manager
# See: docs/operations/credential-rotation.md for rotation procedure

# 3. If OpenAI is fully down (provider incident):
# Activate kill switch for brain-dependent operations
POST /admin/kill-switch
{
  "provider": "openai",
  "mode": "APPROVAL_ONLY",
  "reason": "OpenAI API unavailable — brain operations blocked"
}

# 4. Ollama/NeMo only: Safety gate falls back to pass-through
# Verify Ollama model is loaded
curl http://localhost:11434/api/tags
```

### Escalation
- P1 if production: Core product functionality is broken — Ava cannot respond. Kill switch to APPROVAL_ONLY for all YELLOW/RED operations. Monitor for provider recovery.

---

## Failure Mode 5: Receipt Write Failures

### Symptoms
- `receipt_write_failures_total` counter > 0
- Prometheus alert `ReceiptWriteFailures` fires
- `RECEIPT_WRITE_FAILED` errors in structured logs
- Law #2 compliance at risk

### Diagnosis

```bash
# 1. Immediate: quantify the problem
curl -s http://localhost:8000/metrics | grep receipt_write_failures_total

# 2. Check Supabase connectivity (receipts write to Supabase in production)
curl -s http://localhost:8000/readyz | jq '.checks'

# 3. Query recent failed receipts via admin API
curl -s "http://localhost:8000/admin/ops/receipts?action_type=WRITE_FAILURE&limit=10" \
  -H "X-Admin-Token: <token>" | jq .

# 4. Check correlation IDs for the failing requests
grep "receipt_write_failed" /var/log/aspire-orchestrator.log | tail -20

# 5. Verify receipt chain integrity for affected suite
curl -X POST http://localhost:8000/v1/receipts/verify-run \
  -H "Content-Type: application/json" \
  -d '{"suite_id": "<affected-suite-id>"}'
```

### Resolution

```bash
# 1. If Supabase is down: Fix Supabase connectivity first (see Failure Mode 2)
# In-memory receipt store remains intact — no data is lost during the outage

# 2. If receipt schema mismatch (migration issue):
# Check Supabase migration status
# Run pending migrations: supabase db push

# 3. NEVER delete or update receipts — they are immutable (Law #2)
# Write a correction receipt if incorrect data was committed:
curl -X POST http://localhost:8000/v1/receipts \
  -H "Content-Type: application/json" \
  -d '{
    "receipt_type": "correction",
    "reference_receipt_id": "<original-id>",
    "reason": "Correcting data error in original receipt <id>"
  }'

# 4. After recovery: Replay in-memory receipts to Supabase
# See: docs/operations/replay_trace.md
```

### Escalation
- P0: Any receipt write failure in production is a Law #2 violation. This triggers immediate incident response. Kill switch all YELLOW/RED operations immediately. Open a postmortem within 48 hours.

---

## Escalation Matrix

| Severity | Condition | Response |
|----------|-----------|----------|
| P0 | Receipt write failures in prod, signing key missing, data leak | Acknowledge < 1 hour, kill switch DISABLED |
| P1 | DLP not initialized, Supabase down > 5 min, OpenAI fully down | Acknowledge < 4 hours, kill switch APPROVAL_ONLY |
| P2 | High error rate (> 5%), Redis unavailable, circuit breaker open | Acknowledge < 1 business day |
| P3 | Latency degradation (p95 > 5s), memory pressure | Ticket, fix within 48 hours |

## Kill Switch Activation

```bash
# Activate via Admin API — always generates a receipt (Law #2)
curl -X POST http://localhost:8000/admin/kill-switch \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "suite_id": "<uuid>",
    "provider": "<provider>",
    "mode": "DISABLED",
    "reason": "<incident description>"
  }'
```

See: `backend/docs/operations/kill_switch.md` for full scope options and recovery procedure.
