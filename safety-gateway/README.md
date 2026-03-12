# Aspire Safety Gateway

Separate safety sidecar for the Aspire orchestrator.

Purpose:
- keep the orchestrator runtime decoupled from NeMo Guardrails packaging constraints
- provide a stable `/v1/safety/check` contract
- allow Python 3.13 + NeMo deployment while the orchestrator remains on a newer Python/runtime line

## Modes

- `local`
  - deterministic in-process pattern screening
- `nemo`
  - sidecar-owned NeMo integration path
  - currently falls back to the deterministic local rules when NeMo/config is unavailable

## Environment

- `ASPIRE_SAFETY_GATEWAY_MODE=local|nemo`
- `ASPIRE_SAFETY_GATEWAY_API_KEY=...`
- `ASPIRE_SAFETY_GATEWAY_PORT=8787`

## Bootstrap

Windows:

```powershell
cmd /c scripts\bootstrap_safety_gateway.cmd
```

This creates `safety-gateway/.venv313` using Python 3.13 and installs the package in editable mode.

## Run

```powershell
cmd /c scripts\run_safety_gateway.cmd
```

Smoke check:

```powershell
.\orchestrator\.venv\Scripts\python.exe scripts\smoke_safety_gateway.py
```

## Orchestrator integration

Set in `backend/orchestrator`:

```powershell
$env:ASPIRE_SAFETY_GATEWAY_MODE="remote"
$env:ASPIRE_SAFETY_GATEWAY_URL="http://localhost:8787/v1/safety/check"
$env:ASPIRE_SAFETY_GATEWAY_SHARED_SECRET="your-shared-key"
```

## Deploy

Docker Compose:

```powershell
docker compose -f infrastructure\docker\docker-compose.safety-gateway.yml up --build
```

Full remote wiring with the orchestrator:

```powershell
docker compose -f infrastructure\docker\docker-compose.orchestrator-safety.yml up --build
.\orchestrator\.venv\Scripts\python.exe scripts\smoke_orchestrator_safety_remote.py
```

Production/staging deploy path:

```powershell
.\orchestrator\.venv\Scripts\python.exe scripts\prepare_orchestrator_safety_env.py --environment development --checkpointer memory
cmd /c scripts\deploy_orchestrator_safety_stack.cmd --env-file infrastructure\docker\orchestrator-safety.env
```

For real production:
- copy [orchestrator-safety.env.example](C:\Users\tonio\Projects\myapp\backend\infrastructure\docker\orchestrator-safety.env.example) to `infrastructure/docker/orchestrator-safety.env`
- set `ASPIRE_ENV=production`
- set `ASPIRE_LANGGRAPH_CHECKPOINTER=postgres`
- set `ASPIRE_LANGGRAPH_POSTGRES_DSN`
- then run the same deploy command
