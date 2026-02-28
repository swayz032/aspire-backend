# Aspire Orchestrator — Incident Runbook

## Service Overview

| Property | Value |
|----------|-------|
| **Service** | aspire-orchestrator |
| **Port** | 8000 (WSL2) |
| **Framework** | FastAPI + LangGraph |
| **Runtime** | Python 3.11, venv at `~/venvs/aspire` |
| **Health** | GET /healthz, GET /livez, GET /readyz |
| **Metrics** | GET /metrics (Prometheus) |
| **Logs** | stdout (uvicorn), structured JSON |

## Dependencies

| Dependency | Required | Impact if Down |
|------------|----------|----------------|
| ASPIRE_TOKEN_SIGNING_KEY | Yes | All token mints fail (CAPABILITY_TOKEN_REQUIRED) |
| DLP/Presidio | Yes | /readyz returns 503, receipts not redacted |
| Gateway (:3100) | Yes (upstream) | No requests reach orchestrator |
| Supabase | No (Phase 1) | In-memory receipt store used |
| Redis | No (Phase 1) | No queue processing |

## Health Endpoints

```bash
# Liveness (process alive?)
curl http://localhost:8000/healthz

# Readiness (dependencies configured?)
curl http://localhost:8000/readyz

# Prometheus metrics
curl http://localhost:8000/metrics
```

## Common Failure Modes

### 1. All Executions Denied (CAPABILITY_TOKEN_REQUIRED)

**Symptom:** Every request returns 403 with `CAPABILITY_TOKEN_REQUIRED`.

**Root Cause:** `ASPIRE_TOKEN_SIGNING_KEY` not set or empty.

**Diagnosis:**
```bash
curl http://localhost:8000/readyz | jq '.checks.signing_key_configured'
```

**Resolution:**
```bash
export ASPIRE_TOKEN_SIGNING_KEY="<your-32-char-key>"
# Restart orchestrator
```

### 2. DLP Not Initialized

**Symptom:** /readyz returns `"dlp_initialized": false`. Receipts may contain unredacted PII.

**Diagnosis:**
```bash
curl http://localhost:8000/readyz | jq '.checks.dlp_initialized'
```

**Resolution:**
```bash
pip install presidio-analyzer presidio-anonymizer
# Restart orchestrator — DLP initializes lazily on first use
```

### 3. Safety Gate Blocking All Requests

**Symptom:** All requests return `SAFETY_BLOCKED`.

**Diagnosis:** Check NeMo Guardrails configuration and model availability.

```bash
curl http://localhost:11434/api/tags  # Check Ollama models available
```

**Resolution:** Ensure Ollama is running with a loaded model. Safety gate falls back to pass-through if model is unavailable.

### 4. CORS Rejection

**Symptom:** Browser requests fail with CORS errors.

**Diagnosis:** Check `ASPIRE_CORS_ORIGINS` environment variable.

**Resolution:**
```bash
export ASPIRE_CORS_ORIGINS="http://localhost:3100,http://127.0.0.1:3100"
```

## Restart Procedure

```bash
# WSL2
cd /mnt/c/Users/tonio/Projects/myapp
source ~/venvs/aspire/bin/activate

# Stop existing process
pkill -f "uvicorn.*aspire_orchestrator" || true

# Start
cd backend/orchestrator
python -m uvicorn aspire_orchestrator.server:app --host 0.0.0.0 --port 8000 --reload

# Verify
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

## Log Analysis

### Correlation ID Tracing

Every request gets a correlation_id. Trace a full request lifecycle:

```bash
# Find all log entries for a correlation_id
grep "correlation_id=abc12345" /var/log/aspire-orchestrator.log

# Check receipt chain for the request
curl "http://localhost:8000/v1/receipts?suite_id=<suite>&correlation_id=<corr_id>"
```

### Error Rate Monitoring

```bash
# Check error rate from Prometheus
curl -s http://localhost:8000/metrics | grep aspire_orchestrator_requests_total
```

## Escalation Matrix

| Severity | Condition | Action |
|----------|-----------|--------|
| **P0** | All requests denied, signing key missing | Restore signing key, restart |
| **P1** | DLP not initialized, PII leak risk | Fix Presidio install, restart |
| **P2** | High error rate (>5%) | Check logs, identify failing node |
| **P3** | Latency degradation (p95 > 5s) | Check Ollama model load, safety gate |

## Circuit Breaker States

The orchestrator uses fail-closed semantics (Law #3):
- Missing signing key → All token mints denied
- Missing approval evidence → Yellow/Red tier denied
- Missing presence token → Red tier denied
- DLP failure → Receipt write continues (logs warning)
