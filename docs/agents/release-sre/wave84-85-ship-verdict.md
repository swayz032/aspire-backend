# Waves 8.4 + 8.5 — PRR-Lite Ship Verdict

**Date:** 2026-03-15
**Reviewer:** Release SRE Agent
**Scope:** Wave 8.4 (Prometheus Alert Rules) + Wave 8.5 (Incident Runbooks — 6 documents)
**Artifacts Reviewed:**
- `backend/infrastructure/docker/prometheus/alert_rules.yml` (NEW)
- `backend/infrastructure/docker/prometheus/prometheus.yml` (NEW)
- `backend/docs/operations/runbook.md` (NEW)
- `Aspire-desktop/docs/operations/runbook.md` (NEW)
- `Aspire-Admin-Portal/docs/operations/runbook.md` (NEW)
- `docs/operations/postmortem-template.md` (NEW)
- `docs/operations/rollback-procedure.md` (NEW)
- `docs/operations/backup-restore.md` (NEW)
**Prior PRR context:** Phase 3 Group B (2026-02-16) — SHIP WITH CONDITIONS, 5 conditions identified

---

## EXECUTIVE SUMMARY

**Ship Recommendation: SHIP WITH CONDITIONS**

Waves 8.4 and 8.5 deliver a comprehensive operational documentation layer that directly addresses Gate 4 (Operations) gaps identified in every prior PRR. The alert rules are well-structured and tied to real SLOs defined in `SLI_SLO.md`. The runbooks reference actual file paths, real ports, real environment variable names, and real failure patterns observed in the codebase — this is not boilerplate. However, three operational gaps remain that must be tracked as conditions.

---

## SECTION 1: PRR-LITE FILLED CHECKLIST

### 1. Monitoring and Alerting for User-Visible Failures

**Rating: PARTIAL**

**Evidence — what is adequate:**
- `backend/infrastructure/docker/prometheus/alert_rules.yml`: Six alert groups defined covering error rate, latency, receipt write failures, circuit breaker state, queue depth, memory, burn rate, service availability, readiness, and token mint failures.
- Alert thresholds are tied to documented SLOs from `backend/docs/observability/SLI_SLO.md`: error rate < 1%, p95 latency < 2s, receipt write failures = 0.
- Burn rate alerting implemented: 14.4x (P0 page) and 6x (P1 page) windows per SLI_SLO.md spec.
- `OrchestratorDown` and `DesktopServerDown` alerts cover the two primary user-visible services.
- `OrchestratorNotReady` covers the readiness degradation scenario (DLP missing, signing key missing).
- Existing `otel/alert_rules.yml` covers `OutboxQueueStuck` and `ApprovalLatencyHigh` — Wave 8.4 complements rather than duplicates.
- Grafana provisioning exists at `backend/infrastructure/docker/otel/grafana-provisioning/` with dashboard and datasource config.
- `backend/docs/observability/SLI_SLO.md` defines SLOs for all three services (Backend, Desktop, Admin Portal).

**Gaps:**
- The new `prometheus/alert_rules.yml` and `prometheus/prometheus.yml` live in a NEW `prometheus/` directory. The `docker-compose.observability.yml` still mounts the `otel/` path. These new files will not be active until `docker-compose.observability.yml` is updated to reference the new path — or the new files are merged into the existing `otel/alert_rules.yml`.
- No Alertmanager configuration exists (no receiver routes, no Slack/PagerDuty webhooks configured). The `prometheus.yml` `alerting:` stanza has empty targets. Alerts fire in Prometheus but go nowhere.
- Admin Portal has no server-side metrics endpoint (it is a static SPA). p95 page load SLO is defined in `SLI_SLO.md` but unmeasurable without browser-side instrumentation (RUM). This is not a blocker but is a known gap.
- `DesktopServerDown` alert references `up{job="aspire-desktop"}` but the new `prometheus/prometheus.yml` is not yet wired into any compose file, so the scrape target does not exist in the running Prometheus.

### 2. Logging and Traceability (Flight Recorder)

**Rating: PARTIAL**

**Evidence — what is adequate:**
- `backend/docs/operations/runbook.md`: Correlation ID tracing section with actual curl commands referencing real endpoints (`/v1/receipts`, `/admin/ops/receipts`).
- `Aspire-desktop/docs/operations/runbook.md`: Correlation ID tracing section showing how `x-correlation-id` flows from desktop server through to the backend. Evidence base: `server/index.ts` middleware at lines 103-119 (verified in review).
- Postmortem template (`docs/operations/postmortem-template.md`): Dedicated "Receipt Evidence Trail" section with query commands. Laws Affected table covers all 9 relevant laws. Explicitly ties postmortem to production gate reviews.
- Rollback procedure (`docs/operations/rollback-procedure.md`): Covers receipt immutability — rollbacks do not modify the receipts table. Correction receipt pattern documented.
- Backup/restore procedure (`docs/operations/backup-restore.md`): Dedicated "Receipt Table Special Rules (Law #2)" section. Post-restore receipt chain verification command included.
- Prior reviews confirmed structured JSON logging via `server/logger.ts` and PII redaction via Presidio DLP.

**Gaps:**
- The backend runbook references `/var/log/aspire-orchestrator.log` for log grep commands. Verified the orchestrator logs to stdout (uvicorn), not to a file at that path. If Railway or Docker captures stdout, the path would differ. The grep commands should use `railway logs` or `docker logs` instead.
- Log retention policy is documented in the backend runbook as "30 days minimum" but no enforcement mechanism (log rotation config, Railway log retention config) is referenced. This is a documentation gap, not a code gap.
- Admin portal runbook correctly identifies that `VITE_*` vars are build-time embedded, but does not cover what happens to browser console logs (which may contain PII from API error messages). The prior PRR flagged `console.log('Audio preview request:', { businessName })` at `routes.ts L397` — this is not addressed in the runbooks.

### 3. Safe Deploy and Rollback Steps

**Rating: ADEQUATE**

**Evidence:**
- `docs/operations/rollback-procedure.md`: Covers all three services with Option A (git revert — preferred) and Option B (Railway redeploy to previous deployment). Per-service post-rollback verification checklists. Blast radius assessed. Kill switch as behavioral rollback without code change.
- Estimated time to rollback documented: Backend 2-4 min, Desktop 3-5 min, Admin Portal 2-4 min.
- `docs/operations/backup-restore.md`: Pre-migration manual backup procedure with pg_dump command. S3 upload for durability.
- Stripe webhook rollback consideration documented (replay missed events via Stripe dashboard).
- `VITE_*` build-time embedding caveat documented — env var fix requires full rebuild, not just restart.
- Communication protocol for rollback included: announce → execute → verify → confirm → postmortem.
- Receipt immutability law explicitly referenced throughout: rollbacks do not touch the receipts table.
- Database migration rollback guidance: forward-only migrations, correction migration pattern, pre-migration backup.

**Gaps:**
- Rollback procedure is prescriptive but not tested. Prior PRR (2026-02-16) flagged this same issue. No rollback drill evidence exists. This is a Gate 4 gap that should be scheduled.
- Canary/staged rollout strategy is not defined. Single-instance Railway deployment means 100% blast radius on any failed deploy. This was flagged in prior PRR and remains unresolved.
- Feature flags per-service are not implemented. Kill switch covers provider-level behavioral changes; there is no per-feature toggle for code changes.

### 4. Dependency Failure Behavior

**Rating: ADEQUATE**

**Evidence:**
- `Aspire-desktop/docs/operations/runbook.md`: Failure Mode 2 covers circuit breaker open state with actual circuit breaker thresholds from `server/circuitBreaker.ts` (verified: cockatiel library, ConsecutiveBreaker thresholds per service: backend 5/30s, stripe 3/120s).
- Circuit breakers are IMPLEMENTED in production code (`server/circuitBreaker.ts`) — this addresses the CRITICAL gap from Phase 3 Group B PRR (2026-02-16 Condition 4 was about adding a circuit breaker; it appears it was added via cockatiel in the shipping version).
- `SLI_SLO.md` documents timeout values and circuit breaker thresholds for all external providers (Supabase 5s read/10s write, Stripe 10s, OpenAI 30s, etc.).
- Backend runbook covers all five failure modes with explicit kill switch activation commands referencing the real `/admin/kill-switch` endpoint.
- Desktop runbook covers `DEV_BYPASS_AUTH` production verification command — prevents silent auth bypass in production.
- Supabase fail-closed behavior: `server/index.ts` lines 143-148 show 503 `AUTH_UNAVAILABLE` when supabaseAdmin is null — this is verified in the current code (the Phase 3 Group B Condition 3 fix was applied).

**Gaps:**
- The runbooks describe what to do when a circuit breaker opens but do not cover the thundering herd problem: when a circuit breaker closes and all held requests retry simultaneously. This is a known operational risk.
- Dependency health monitoring is described at the runbook level but no `/api/dependencies/health` composite endpoint exists in the desktop server. This was recommended in the prior PRR as a non-blocking advisory and remains unimplemented.
- OpenAI rate limiting (429) recovery procedure in the backend runbook is correct in principle but the 120s circuit breaker open time may be too short for OpenAI rate limit windows (which can be 60s per-minute or up to 1 hour for daily limits).

### 5. Capacity, Timeouts, Retries, and Idempotency

**Rating: PARTIAL**

**Evidence:**
- Runbooks document timeout values per `SLI_SLO.md`: Supabase 5s read, 10s write; Stripe 10s; OpenAI 30s; ElevenLabs 15s.
- Retry policy documented: exponential backoff with jitter in cockatiel (3 attempts, 500ms initial, 10s max).
- Capability token TTL (<60s) referenced in backend runbook under kill switch activation.
- `docs/operations/backup-restore.md` documents RTO targets per scenario: config change < 5 min, code regression < 10 min, PITR < 60 min.
- Load test script exists at `backend/orchestrator/scripts/load_test.py` — referenced in `backend/docs/operations/load-test-report.md`.

**Gaps:**
- Load test has NOT been executed against the current codebase. The `load-test-report.md` shows the script is ready but "Live soak results will be appended after execution" — this note has not been fulfilled. Capacity under production load is unknown.
- The backend runbook does not specify connection pool sizing for the Drizzle ORM pool in `server/db.ts`. The prior PRR flagged pool exhaustion as a risk; the runbook describes symptoms of pool exhaustion (Failure Mode 2) but not the pool configuration values.
- Idempotency for approve/deny operations: The current `server/routes.ts` has not been verified to include idempotency keys post-Phase 3 Group B shipping. The prior PRR flagged this as a medium-priority advisory. The runbooks do not address idempotency testing.
- Queue depth is monitored via `outbox_queue_depth` metric and the `QueueDepthHigh` alert, but backpressure mechanism (rejecting new requests when queue is full) is not described in the runbooks and may not be implemented.

---

## SECTION 2: HIGHEST-RISK OPERATIONAL GAPS

### Risk 1: Alert Rules Are Not Wired Into the Running Prometheus Instance

**Severity: HIGH**

**What:** The new alert rules file is at `backend/infrastructure/docker/prometheus/alert_rules.yml`. The running Prometheus instance is configured by `backend/infrastructure/docker/otel/prometheus.yml` and is managed by `docker-compose.observability.yml`. The new `prometheus/` directory is not mounted by any compose file. All Wave 8.4 alerts are currently inert — they fire nowhere.

**Impact:** Receipt write failure alerts (`ReceiptWriteFailures`), orchestrator down alerts (`OrchestratorDown`), and error budget burn alerts will never fire until the compose configuration is updated. Gate 2 (Observability) remains incomplete despite the effort.

**Mitigation:** Either (a) merge Wave 8.4 alert rules into `backend/infrastructure/docker/otel/alert_rules.yml` and add the new rule group to the existing file, or (b) update `docker-compose.observability.yml` to mount the new `prometheus/alert_rules.yml` and use the new `prometheus/prometheus.yml`. Option (a) is simpler and avoids directory fragmentation.

**Aspire Law Affected:** Production Gate 2 (Observability — SLO alerts must be active).

### Risk 2: No Alertmanager Configuration — Alerts Fire but Reach No One

**Severity: HIGH**

**What:** `prometheus/prometheus.yml` has an `alerting:` block with empty `targets: []`. This means Prometheus evaluates alert rules and generates alerts, but has nowhere to route them. There is no Alertmanager deployment, no Slack webhook configured, no PagerDuty integration.

**Impact:** The `ReceiptWriteFailures` alert (Law #2 violation risk, severity: page) and `OrchestratorDown` (severity: page) would evaluate and remain in the Prometheus alert queue with no notification to on-call engineers. This makes all alerting theater — it exists on paper but produces no action.

**Mitigation:** Deploy Alertmanager as a service in `docker-compose.observability.yml`. Configure at minimum one receiver: a Slack webhook to `#aspire-ops`. Add the alertmanager endpoint to `prometheus.yml` `alerting.alertmanagers[0].static_configs[0].targets`. This is a production prerequisite for Gate 2.

**Aspire Law Affected:** Production Gate 2 (Observability — health checks and alerts must route to on-call).

### Risk 3: Load Test Not Executed — Capacity Unknown

**Severity: HIGH**

**What:** `backend/docs/operations/load-test-report.md` shows the load test script was validated on 2026-02-13 but live soak results were explicitly deferred: "Live soak run completed (deferred — requires running orchestrator instance)." This same gap was flagged in the Phase 3 Group B PRR (Condition 2, marked BLOCKING) on 2026-02-16. It remains unresolved.

**Impact:** SLO compliance under production load is unknown. If the system cannot sustain 100 concurrent requests at p95 < 2s, the SLOs defined in `SLI_SLO.md` are aspirational rather than validated. A production traffic spike could expose capacity limits that have never been measured.

**Mitigation:** Execute load test as defined in `load-test-report.md`: `python scripts/load_test.py --duration 600 --rate 100`. Document results. If p95 > 2s or error rate > 1%, identify the bottleneck before production deployment.

**Aspire Law Affected:** Production Gate 3 (Reliability — timeout enforcement and capacity planning).

### Risk 4: Backend Runbook References Non-Existent Log File Path

**Severity: MEDIUM**

**What:** `backend/docs/operations/runbook.md` includes `grep "correlation_id=abc12345" /var/log/aspire-orchestrator.log`. The orchestrator logs to stdout (uvicorn), not to `/var/log/aspire-orchestrator.log`. In Railway or Docker, logs are captured via container stdout, not written to a filesystem path.

**Impact:** At 3 AM during an incident, on-call engineers will execute this command and get "No such file or directory." This erodes trust in the runbook and causes delay. Runbooks that fail on first use are worse than no runbooks.

**Mitigation:** Replace the log grep commands with the correct commands for each deployment context: `railway logs --service aspire-backend | grep <correlation-id>` for production; `docker logs aspire-orchestrator | grep <correlation-id>` for Docker; direct stdout for WSL2 local development.

**Aspire Law Affected:** Production Gate 4 (Operations — runbooks must be executable during incidents).

### Risk 5: Rollback Procedure Has Never Been Tested

**Severity: MEDIUM**

**What:** This gap was first flagged in the Phase 3 Group B PRR (2026-02-16). The rollback procedure is documented in `docs/operations/rollback-procedure.md` and extended in this wave. The 2-4 minute recovery time estimate is theoretical. No rollback drill has been conducted.

**Impact:** An untested rollback procedure under real incident conditions (elevated stress, potential dependency failures, possible schema migration complications) may take significantly longer than 4 minutes, or fail entirely. The first time a rollback is executed should not be during a production P0.

**Mitigation:** Schedule a rollback drill: introduce a deliberate non-breaking regression in a staging branch, deploy it, verify the failure, execute the rollback procedure, and measure actual recovery time. Document the result. Target: complete before next production feature deployment.

**Aspire Law Affected:** Production Gate 4 (Operations — rollback procedures tested, not just documented).

---

## CROSS-CHECK AGAINST ASPIRE PRODUCTION GATES

| Gate | Status | Evidence |
|------|--------|----------|
| GATE 1: Testing | NOT IN SCOPE | Wave 8.4/8.5 is operational documentation only — no new code paths that require testing. Existing test baseline carries forward from Phase 3 Enterprise Remediation (3061 tests). |
| GATE 2: Observability | PARTIAL | Alert rules written and comprehensive. SLO alignment confirmed. CRITICAL: New alert rules not mounted in running Prometheus. No Alertmanager configured — alerts reach no one. Existing Grafana dashboard provisioning is in place. |
| GATE 3: Reliability | PARTIAL | Circuit breakers confirmed implemented (cockatiel, `server/circuitBreaker.ts`). Timeout values documented per `SLI_SLO.md`. Load test not executed — capacity unknown. This gap predates Wave 8.4/8.5 but is not resolved. |
| GATE 4: Operations | ADEQUATE | Six operational documents created. Runbooks reference real architecture (verified ports, env vars, file paths). Postmortem template tied to Aspire Laws and Production Gates. Rollback procedure covers all three services with Railway-specific commands. Backup/restore covers Supabase PITR and RLS verification. GAPS: Log path error in backend runbook, rollback procedure untested. |
| GATE 5: Security | NOT IN SCOPE | No security-relevant changes in this wave. Prior security controls (RLS, JWT, DLP, capability tokens) carry forward. `DEV_BYPASS_AUTH` production check documented in desktop runbook. |

---

## SECTION 3: SHIP / NO-SHIP RECOMMENDATION

### SHIP WITH CONDITIONS

**Rationale:**

Waves 8.4 and 8.5 represent a substantial and well-executed improvement to Gate 4 (Operations) compliance. The runbooks are grounded in the actual codebase — they cite real ports, real environment variable names, real command patterns, and real failure modes observed during the review. The postmortem template integrates Aspire Laws directly. The rollback and backup procedures cover operational reality, not ideal theory.

The conditional status is driven by two issues that prevent these artifacts from functioning in production: the alert rules are inert (not mounted in the running Prometheus), and Alertmanager is absent (alerts reach no one). These are not gaps in the documentation — the documentation is correct. These are gaps in the infrastructure that must be closed before Wave 8.4 delivers its intended value.

**Conditions (must resolve before claiming Gate 2 compliance):**

**Condition 1 — Wire alert rules into running Prometheus (BLOCKING for Gate 2)**
Merge `prometheus/alert_rules.yml` content into `backend/infrastructure/docker/otel/alert_rules.yml`, or update `docker-compose.observability.yml` to mount the new file. Verify with `curl http://localhost:9090/api/v1/rules | jq '.data.groups[].name'` — should show `aspire_critical`, `aspire_slo_burn`, `aspire_availability`, `aspire_governance`.
Deadline: Before next production deployment.

**Condition 2 — Deploy Alertmanager with at minimum one Slack receiver (BLOCKING for Gate 2)**
Add Alertmanager to `docker-compose.observability.yml`. Configure `alertmanager.yml` with a Slack receiver for `#aspire-ops` channel. Update `prometheus/prometheus.yml` (and `otel/prometheus.yml`) `alerting.alertmanagers` targets. Verify with a test alert: `curl -X POST http://localhost:9093/api/v1/alerts -d '[{"labels":{"alertname":"TestAlert","severity":"warning"}}]'` — confirm Slack receives the message.
Deadline: Before next production deployment.

**Non-blocking recommendations (address within 2 weeks):**

- Fix log path in `backend/docs/operations/runbook.md`: Replace `/var/log/aspire-orchestrator.log` grep commands with `railway logs` equivalents.
- Execute load test: `python scripts/load_test.py --duration 600 --rate 100`. Document results in `backend/docs/operations/load-test-report.md`. This resolves the Risk 3 gap that has been open since 2026-02-13.
- Schedule rollback drill: One deliberate regression → deploy → rollback → document actual recovery time.
- Consider merging the `prometheus/` directory into `otel/` to avoid configuration fragmentation. Single source of truth for Prometheus configuration.

**Confidence level: HIGH**

The assessment is based on direct code review of all referenced files. Circuit breaker implementation was verified at `server/circuitBreaker.ts`. Fail-closed auth behavior was verified at `server/index.ts` lines 143-148. SLO alignment was verified against `backend/docs/observability/SLI_SLO.md`. The one caveat lowering from VERY HIGH is the unverified load test execution — actual capacity under load remains unknown.

---

**END OF PRR-LITE SHIP VERDICT**

_SRE Reviewer: Release SRE Agent_
_Report Date: 2026-03-15_
_Next Review: After Alertmanager deployment and alert rule wiring confirmed_
