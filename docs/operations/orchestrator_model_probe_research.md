# Orchestrator Model Probe Degradation Research

Date: 2026-03-14  
Scope: readiness degradation on `orchestrator /readyz` where `model_probe_healthy=false`

## Observed Runtime State

- `GET /healthz` returns `ok`
- `GET /readyz` returns `degraded`
- Ready payload includes:
  - `model_probe_cache=true`
  - `model_probe_healthy=false`
- models in probe cache all `false`

Validated with runtime probe:
- `ASPIRE_OPENAI_API_KEY` existed in container but produced OpenAI `401 invalid_api_key`
- After syncing the key from Railway server-side vars and restarting compose, model probe returned healthy

## Code-Level Root Cause

1. `probe_models_startup()` probes configured model chain and marks each model availability in `_MODEL_PROBE_CACHE`.  
2. Probe health is `healthy = any(_MODEL_PROBE_CACHE.values())`.  
3. If all probed models fail, `readyz` becomes `degraded` even when critical checks pass.

Relevant code:

- `backend/orchestrator/src/aspire_orchestrator/services/openai_client.py`
  - `probe_models_startup()`
  - `get_model_probe_status()`
- `backend/orchestrator/src/aspire_orchestrator/server.py`
  - `/readyz` check population and final status mapping

## Confirmed Root Cause

- The orchestrator was using a stale/invalid OpenAI key in local docker env (`ASPIRE_OPENAI_API_KEY`), causing startup probes for `gpt-5.2`, `gpt-5`, `gpt-5-mini` to fail.
- Key source drift existed between local env and Railway/AWS secret source-of-truth.

## Likely Environmental Causes

- Missing/invalid `ASPIRE_OPENAI_API_KEY` (or missing `OPENAI_API_KEY` fallback)
- Network egress / DNS / firewall issues reaching `ASPIRE_OPENAI_BASE_URL`
- Account/model access mismatch for configured router models (`gpt-5`, `gpt-5-mini`, `gpt-5.2`)
- Timeout too low for environment (`ASPIRE_OPENAI_TIMEOUT_SECONDS`)

## Enterprise-Grade Remediation Path

1. **Validate secrets and endpoint**
   - Ensure `ASPIRE_OPENAI_API_KEY` is present and non-empty in runtime env.
   - Ensure `ASPIRE_OPENAI_BASE_URL` is correct for deployed environment.

2. **Validate model contract**
   - Confirm configured models are enabled for the account:
     - `ASPIRE_ROUTER_MODEL_GENERAL`
     - `ASPIRE_ROUTER_MODEL_CLASSIFIER`
     - `ASPIRE_ROUTER_MODEL_REASONER`
     - `ASPIRE_ROUTER_MODEL_HIGH_RISK`

3. **Adjust probe timeout**
   - Raise `ASPIRE_OPENAI_TIMEOUT_SECONDS` if env latency is high.

4. **Keep readiness strict**
   - Do not bypass model probe health in production.
   - Gateway should consume orchestrator `readyz` (not `healthz`) for readiness gating.

## Verification Checklist

- [x] `orchestrator /readyz` returns `status=ready`
- [x] `model_probe_healthy=true`
- [x] At least one router model in probe cache reports `true`
- [ ] `gateway /readyz` returns `ready` and `dependencies.orchestrator=healthy`
