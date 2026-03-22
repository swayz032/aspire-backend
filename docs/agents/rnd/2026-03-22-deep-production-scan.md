# Deep Production Scan: Aspire Backend

## Date: 2026-03-22
## Classification: Analysis
## Risk Tier: YELLOW

## Executive Summary

Deep scan of the Aspire backend orchestrator at `backend/orchestrator/src/aspire_orchestrator/`. All critical import chains resolve. No broken module references found. Two security findings (QBO query injection, env var mismatch), one config mismatch that silently degrades startup validation, stale artifacts, and one architectural concern (4,600-line admin route file). Overall: the codebase is well-structured with strong governance patterns, but has specific issues that need attention.

---

## CRITICAL (Must Fix Before Production)

### C1. QuickBooks Query Language Injection
- **File**: `providers/quickbooks_client.py`, lines 428-430
- **Issue**: `account_type` from user payload is interpolated directly into a QBO Query string without validation:
  ```python
  query = f"SELECT * FROM Account WHERE AccountType = '{account_type}'"
  ```
- **Contrast**: The same file properly validates `start_date` and `end_date` with regex (lines 311-315) but skips validation for `account_type`.
- **Risk**: Attacker can inject arbitrary QBO query clauses. While this is QBO QL (not Postgres SQL), it can expose data from other QBO entities or bypass intended filters.
- **Fix**: Add an allowlist of valid QBO AccountType values (Bank, Expense, Revenue, etc.) and reject anything not in the list.

### C2. PandaDoc Webhook Secret Env Var Mismatch
- **Files**: `routes/webhooks.py` line 121 vs `config/settings.py` line 74
- **Issue**: Settings defines `pandadoc_webhook_secret` (env var: `ASPIRE_PANDADOC_WEBHOOK_SECRET`), but the webhook handler reads `PANDADOC_WEBHOOK_KEY` — a completely different env var name. Neither the `KEY_MAP` nor `_SETTINGS_PREFIX_MAP` in `config/secrets.py` bridges this gap.
- **Risk**: PandaDoc webhook signature verification silently fails if only `ASPIRE_PANDADOC_WEBHOOK_SECRET` is set (the handler never sees it). In production, this means webhook events from PandaDoc are rejected, breaking contract status updates.
- **Fix**: Either rename the env var in webhooks.py to match Settings, or add a bridge entry to `_SETTINGS_PREFIX_MAP`.

---

## HIGH (Should Fix Soon)

### H1. verify_settings_coverage References Non-Existent Field
- **File**: `config/secrets.py`, line 140
- **Issue**: `verify_settings_coverage()` checks for `stripe_secret_key` at env var `STRIPE_SECRET_KEY`, but the Settings class defines `stripe_api_key` (env var: `ASPIRE_STRIPE_API_KEY`). These are different Stripe key types (secret key vs restricted API key).
- **Risk**: Startup validation thinks Stripe is unconfigured even when `ASPIRE_STRIPE_API_KEY` is set, producing false warnings. In production (where warnings are fatal), this could block deployment.

### H2. admin.py is 4,635 Lines — God Module
- **File**: `routes/admin.py` — 4,635 lines, 38+ route handlers
- **Issue**: Single file containing all admin ops endpoints: health, incidents, receipts, provider calls, outbox, rollouts, proposals, robots, triage, dashboard, sentry, SSE streams, voice STT/TTS, chat. This makes the file nearly impossible to review, test in isolation, or modify without risk.
- **Risk**: High cognitive load, merge conflicts, accidental regressions. Any change to one endpoint risks breaking others in the same file.
- **Recommendation**: Split into sub-modules: `admin/health.py`, `admin/incidents.py`, `admin/voice.py`, `admin/sse.py`, etc. Each with its own test file.

### H3. Stale `backend/backend/` Duplicate Directory
- **Path**: `backend/backend/orchestrator/src/aspire_orchestrator/providers/pandadoc_client.py` and `backend/backend/orchestrator/tests/test_clara_production.py`
- **Issue**: A duplicated `backend/` directory exists at `backend/backend/`. Contains stale copies of 2 files. This can confuse IDEs, grep results, and CI/CD.
- **Fix**: Delete `backend/backend/` entirely.

### H4. respond_node Blocks Async Event Loop with Sync OpenAI Call
- **File**: `nodes/respond.py` line 525 calls `generate_text_sync()` from `openai_client.py` line 633
- **Issue**: `respond_node` is a sync function that calls `generate_text_sync()`, which makes a synchronous HTTP request to the OpenAI API. When LangGraph runs this node inside the async event loop, it blocks the entire loop for the duration of the API call (up to 30s timeout).
- **Risk**: Under concurrent load, this blocks all other async operations (websockets, health checks, SSE streams) until the OpenAI call completes. Single-user dev doesn't surface this; production with multiple concurrent intents will.
- **Recommendation**: Either make `respond_node` async and use `generate_text_async`, or wrap the sync call in `asyncio.to_thread()`.

---

## MEDIUM (Technical Debt)

### M1. Three Stale `.py.txt` Files in Source Tree
- `services/legal_embedding_service.py.txt`
- `services/base_retrieval_service.py.txt`
- `config/settings.py.txt`
- **Issue**: Old copies of production files sitting in the source tree. Not importable, but pollute grep results and IDE search.
- **Fix**: Delete all three.

### M2. CORS Origins Hardcoded as Fallback
- **File**: `server.py` lines 192-199
- **Issue**: `_CORS_DEFAULTS` includes `http://localhost:3100`, `http://localhost:5173`, production domains. These are used when `ASPIRE_CORS_ORIGINS` env var is unset.
- **Risk**: In production, if `ASPIRE_CORS_ORIGINS` is accidentally unset, localhost origins are allowed — a security concern for credential-bearing requests (`allow_credentials=True`).
- **Recommendation**: In production mode, require `ASPIRE_CORS_ORIGINS` to be explicitly set (fail closed).

### M3. Missing Dedicated Test Files for Critical Services
The following core services lack dedicated test files (may be tested transitively via integration tests):
- `services/orchestrator_runtime.py` — no `test_orchestrator_runtime.py`
- `services/supabase_client.py` — no `test_supabase_client.py`
- `services/intent_classifier.py` — no `test_intent_classifier.py` (only in `test_brain_layer.py`)
- `services/skill_router.py` — no `test_skill_router.py`
- `services/sse_manager.py` — has `test_sse_manager.py` and `test_sse_streaming.py` (verified)
- **Risk**: Transitive test coverage may miss edge cases. Direct unit tests for these critical path modules would catch regressions faster.

### M4. `_check_with_timeout` Uses Bare `coro` Type Annotation
- **File**: `routes/admin.py` line 676 — `coro,  # noqa: ANN001`
- **Issue**: The `coro` parameter has no type annotation (suppressed with noqa). Should be `Coroutine[Any, Any, Any]` or `Awaitable[Any]`.
- **Risk**: Type checker cannot validate callers.

### M5. server.py Module-Level Execution
- **File**: `server.py` lines 237-264
- **Issue**: `load_secrets()`, `init_sentry()`, `_verify_environment_parity()`, and `verify_settings_coverage()` all execute at module import time (not inside the lifespan context). This means importing `server` for testing triggers secret loading and environment validation.
- **Risk**: Test isolation issues — importing server module in tests requires mocking AWS SM, env vars, etc. Makes test setup more complex.

---

## LOW (Nice to Have)

### L1. Inconsistent Secret Resolution Paths
- **Issue**: Some code reads secrets via `settings.X` (Pydantic), some via `os.environ.get()`, some via both with fallback chains. Examples:
  - Token signing: `settings.token_signing_key or os.environ.get("ASPIRE_TOKEN_SIGNING_KEY")` (server.py:356)
  - OpenAI key: `resolve_openai_api_key()` with 3-level fallback (settings.py:138-150)
  - Webhook secrets: Direct `os.environ.get()` in webhooks.py
- **Risk**: Cognitive overhead, potential for one path to have a key while another doesn't. The `_SETTINGS_PREFIX_MAP` bridge helps but isn't comprehensive.

### L2. Admin Deep Health Check Intentionally Unauthenticated
- **File**: `routes/admin.py` lines 771-780
- **Issue**: `/admin/ops/health/deep` exposes detailed dependency status (Postgres, Redis, OpenAI, n8n, etc.) without authentication. While the comment explains the design rationale (observability), this reveals infrastructure topology to unauthenticated callers.
- **Risk**: Information disclosure. Attacker learns which dependencies are up/down and their response times.
- **Recommendation**: Consider limiting deep health to authenticated callers, or at minimum redact dependency names for unauthenticated requests.

### L3. Temporal Integration Files Present but Feature-Flagged
- **Files**: `temporal/` directory with workflows, activities, config, codec, client, worker, interceptors, models
- **Status**: All Temporal features are behind feature flags (`TEMPORAL_INTENT_ENABLED`, `TEMPORAL_APPROVAL_ENABLED`, `TEMPORAL_OUTBOX_ENABLED`), all defaulting to `false`.
- **Observation**: Not dead code — it's a planned integration. But if Temporal is not deployed, these files add ~2,000 lines of unused code to the codebase.

### L4. `_count_by` Uses Untyped Lambda
- **File**: `services/registry.py` line 224 — `def _count_by(items: list[Any], key_fn: Any)`
- **Issue**: `key_fn` is typed as `Any` instead of `Callable[[Any], str]`.

---

## Pipeline Flow Verification

Verified: The LangGraph pipeline is fully wired:
```
Intake -> GreetingCheck -> Safety -> Classify -> Route -> ParamExtract -> PolicyEval -> ApprovalCheck -> TokenMint -> Execute -> ReceiptWrite -> QA -> Respond
```
- All 14 nodes resolve to existing functions
- All conditional edges have correct return mappings
- Skill packs: 19 files in `skillpacks/` directory, all importable
- Registry: YAML-driven at `config/skill_pack_manifests.yaml`, loaded by singleton
- Providers: 17 provider clients, all extending `BaseClient` with circuit breakers

## Security Summary

- Auth enforcement: All 38 admin routes call `_require_admin()` or `_require_incident_reporter()` (except `/admin/ops/health` and `/admin/ops/health/deep` which are intentionally public)
- PII redaction: Sentry `before_send` hook strips 12 categories of sensitive patterns
- Token signing: Fail-closed on missing `ASPIRE_TOKEN_SIGNING_KEY` (Law #3)
- HMAC comparison: Uses `hmac.compare_digest()` (timing-safe)
- No raw SQL against Postgres — all Supabase access via client library
- QBO query injection (C1) is the only injection vector found

## Test Health

- 100+ test files in `tests/` directory
- Test coverage for all skill packs, nodes, middleware, providers
- Evil/security tests: `test_evil_wave7.py`, `test_evil_security.py`, `test_evil_browser_security.py`, temporal evil tests
- RLS isolation tests: `test_rag_rls_isolation.py`
- 5 PandaDoc E2E tests (conditionally skipped via `PANDADOC_E2E` env var)
- 5 Temporal replay tests (conditionally skipped when no history files)
- 1 browser security test skipped (`test_evil_browser_security.py:279` — mock configuration issue)
