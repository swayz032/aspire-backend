# Launch Gate Checklist (Backend + Desktop + Admin)

Last updated: 2026-03-14
Owner lane: Codex (`pipeline sync`, `health/readiness`, `auth/tenant propagation`, `mock removal`, `security/leak prevention`, `launch gate`)

## 1. Backend Spine Gate

- [ ] `gateway /healthz` returns `200`
- [ ] `gateway /readyz` returns `200` (orchestrator healthy)
- [ ] `orchestrator /healthz` returns `200`
- [ ] `orchestrator /readyz` returns `200`
- [ ] `POST /v1/intents` works through gateway with auth context headers
- [ ] Response includes `X-Correlation-Id`
- [ ] Intent path denies malformed/unauthenticated requests (fail closed)

Suggested commands:

```powershell
curl.exe -sS http://localhost:3100/healthz
curl.exe -sS http://localhost:3100/readyz
curl.exe -sS http://localhost:8000/healthz
curl.exe -sS http://localhost:8000/readyz
```

## 2. Desktop Critical Smoke Gate

- [ ] Auth entry works (no mock bypass)
- [ ] Onboarding/bootstrap completes once (idempotent behavior)
- [ ] Ava/orchestrator critical intent path succeeds or returns explicit degraded error
- [ ] Receipts tab loads from real backend data path
- [ ] Inbox tab loads from real backend data path
- [ ] Finance critical path is real (no fabricated demo values on launch-critical view)

## 3. Admin Portal Critical Smoke Gate

- [ ] Receipts page reads live data
- [ ] Incidents page reads live data
- [ ] Provider health shows live/degraded status (no silent empty fallback)
- [ ] Critical operations path uses tenant-scoped backend data

## 4. Anti-Leak / Tenant Isolation Gate

- [ ] Forged client `suite_id` in request body cannot override auth context
- [ ] Forged client `office_id` in request body cannot override auth context
- [ ] Cross-tenant receipt query attempt is denied
- [ ] Missing auth context paths fail closed

Evidence targets:

- Gateway tenant override tests in `backend/gateway/src/__tests__/routes.test.ts`
- Runtime smoke logs with correlation IDs and denial responses

## 5. Deploy + Env Parity Gate

- [ ] No launch-critical process depends on hidden localhost defaults in production
- [ ] Required env vars are explicit and validated before startup
- [ ] Staging/prod env contracts are aligned for gateway, orchestrator, desktop, admin

Minimum env contracts:

- Gateway: `ORCHESTRATOR_URL`, `SUPABASE_JWT_SECRET` (or equivalent auth config)
- Desktop web app: `EXPO_PUBLIC_SUPABASE_URL`, `EXPO_PUBLIC_SUPABASE_ANON_KEY`
- Orchestrator: auth + provider contracts required by enabled lanes
- Admin portal: ops facade + auth contracts required for live provider/incident views

## Gate Result

Record one row per run:

| Date (UTC) | Backend Spine | Desktop Smoke | Admin Smoke | Anti-Leak | Env Parity | Verdict | Evidence |
|---|---|---|---|---|---|---|---|
| YYYY-MM-DD | pass/fail | pass/fail | pass/fail | pass/fail | pass/fail | green/red | links/log paths |
| 2026-03-14 | fail | fail | fail | fail | fail | red | `gateway :3100 unreachable`; `orchestrator /healthz=ok`; `orchestrator /readyz=degraded (model_probe_healthy=false)` |
| 2026-03-14 (run 2) | fail | fail | fail | fail | fail | red | `gateway /healthz=200`; `gateway /readyz=ready`; `orchestrator /healthz=ok`; `orchestrator /readyz=degraded (model_probe_healthy=false)`; `POST /v1/intents=200` with forged body tenant fields |
| 2026-03-14 (run 3) | fail | fail | fail | fail | fail | red | `gateway /readyz=degraded` now mirrors `orchestrator /readyz=degraded`; `model_probe_healthy=false` remains unresolved |
| 2026-03-14 (run 4) | fail | fail | fail | pass | pass | red | `orchestrator /readyz=ready`; `model_probe_healthy=true` after Railway key sync; gateway runtime not continuously up on `:3100` in this session |
