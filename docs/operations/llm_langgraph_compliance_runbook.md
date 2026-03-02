# LLM + LangGraph Compliance Runbook

## Purpose
Operate and triage Aspire orchestrator model-routing and LangGraph persistence issues in production.

## Required Production Env (Ava Brain / Orchestrator)
- `ASPIRE_LANGGRAPH_CHECKPOINTER=postgres`
- `ASPIRE_LANGGRAPH_POSTGRES_DSN=<postgres dsn>`
- `ASPIRE_OPENAI_API_KEY=<key>`
- `ASPIRE_OPENAI_USE_CHAT_FALLBACK=0` (recommended)
- `ASPIRE_MODEL_FALLBACK_MAP=<optional json>`

## Health Verification
1. `GET /healthz` must return `status=ok`.
2. `GET /readyz` checks:
- `langgraph_checkpointer=true`
- `langgraph_checkpoint_store=true`
- `model_probe_cache=true`
- `model_probe_healthy=true`
3. Confirm `checkpointer.mode=postgres` in `/readyz` payload.

## Primary Failure Codes
- `MODEL_UNAVAILABLE`: model/provider rejects configured model.
- `UPSTREAM_TIMEOUT`: OpenAI request timed out.
- `CHECKPOINTER_UNAVAILABLE`: checkpoint backend missing/unreachable.
- `ROUTER_FALLBACK_ACTIVE`: model/profile fallback path currently active.

## Triage Steps
1. Check `/readyz` for checkpointer and model probe status.
2. Check deployment env values for missing/invalid model config.
3. Inspect metrics:
- `llm_request_total{endpoint,resolved_model,outcome}`
- `llm_model_fallback_total{profile,from_model,to_model}`
4. Validate recent logs include:
- `correlation_id`
- `thread_id`
- `agent_target`
- `model_profile`
- `resolved_model`
- `fallback_used`
5. If checkpointer is degraded:
- verify DSN connectivity and credentials
- restart service after DSN fix
6. If model probe is unhealthy:
- verify OpenAI key
- verify model access in account
- confirm fallback map JSON is valid

## Rollback
1. Revert to prior release.
2. Keep `ASPIRE_LANGGRAPH_CHECKPOINTER=postgres` in production.
3. Re-run `/readyz` and confirm all critical checks are healthy.

## Post-Incident
1. Capture correlation IDs and affected profiles/models.
2. Export fallback metric deltas.
3. Document root cause and permanent fix in incident timeline.
