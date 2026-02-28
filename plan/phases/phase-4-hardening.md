---
phase: "4"
name: "Production Certification (11 Unified Gates)"
status: "not_started"
blocking_phase: "3"
blocks_phases: ["5"]
duration_estimate: "8-10 weeks (accelerated via ADR-0008 pre-built release gates)"
gates_targeted: [8, 9, 10]
priority: "critical"
hardware_required: "Skytech Shadow"
cost: "$40-60/mo (Sentry, load testing)"
---

# PHASE 4: Production Hardening (10/10 Bundle)

## 🔗 HARDENING WIRING TASKS (v4.2)

**This phase hardens all 19 API connections for production:**

| API Service | Hardening Task | Pattern | Verification |
|-------------|----------------|---------|--------------|
| **All 19 APIs** | Circuit breakers | Resilience4j | Graceful degradation |
| **All 19 APIs** | Exponential backoff | Jitter + backoff | No thundering herd |
| **All 19 APIs** | Timeout enforcement | <5s tools, <30s orch | No hung requests |
| **Stripe** | Idempotency keys | Outbox pattern | No duplicate charges |
| **All financial** | Dual approval | Video + confirmation | RED tier secured |

**Reliability Engineering:**
```
Circuit Breaker Config:
├── Failure threshold: 50% of last 10 requests
├── Recovery timeout: 30 seconds
├── Half-open trial: 3 requests
└── Fallback: Return cached/error with receipt

Backoff Config:
├── Initial delay: 1 second
├── Max delay: 60 seconds
├── Multiplier: 2.0
├── Jitter: 0.1-0.5
└── Max retries: 5
```

**4 Internal Skill Packs to Wire:**
- `sre_triage` → Incident automation
- `qa_evals` → QA evaluation automation
- `security_review` → Security review automation
- `release_manager` → Release management automation

**Files to Create:**
- `tools/validators/sync-validator.ts` → Automated sync checking
- `.github/workflows/sync-validation.yml` → CI/CD sync check

**All 11 Production Gates MUST Pass:**
1. Gate 0: Capability Token Enforcement
2. Gate 1: Intent-to-Receipt Path
3. Gate 2: Call State Persistence
4. Gate 3: Forced Escalation
5. Gate 4: Network-Failure Degradation
6. Gate 5: Cold/Warm/Hot Authority UI
7. Gate 6: Receipts Immutable
8. Gate 7: RLS Isolation
9. Gate 8: Performance Baseline
10. Gate 9: Smoke Tests
11. Gate 10: Deterministic Replay

---

## Objective

Achieve production readiness via 11 unified production gates (Trust Spine + Aspire).

**Timeline Savings:** ADR-0008 Release Gates pre-defines certification framework, saves 1 week.

---

## Trust Spine Ecosystem Documentation (IMPORTANT)

**📚 Production certification and deployment documentation exists in the Trust Spine package:**

### Production Gates Resources
- **Navigation Guide:** See `Trust-Spine-Package-Navigation.md` (in this plan directory) for production deployment workflow
- **ADR-0008:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/ADR-0008-release-governance.md` for 11 unified production gates specification
- **Definition of Done:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/PHASE_5_LAUNCH_GOVERNANCE/DEFINITION_OF_DONE/` for production gate checklist

### Production Deployment Resources
- **Release Checklist:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_5_LAUNCH_GOVERNANCE/TEMPLATES/RELEASE_CHECKLIST.md` for:
  - Pre-deploy checklist (migration review, backup verification)
  - Deploy steps (migrations → Edge Functions → Go service)
  - Post-deploy monitoring (error rates, latency, RLS violations)
  - Rollback procedures (in case of deployment failure)
- **Production Runbook:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_5_LAUNCH_GOVERNANCE/RUNBOOKS/` for production deployment guide

### Testing & Validation Resources
- **E2E Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/` for all production gate validation tests
- **Load Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/14_STRESS_TESTS/k6/` for k6 stress tests (Gate 8: 1000 req/s sustained, p95 <500ms)
- **Test Execution Guide:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/README.md` for running all 11 gates systematically

### Observability & Incident Response
- **SLI/SLO Definitions:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/ops/SLI_SLO.md` for production monitoring metrics
- **Incident Runbooks:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/ops/runbooks/` for:
  - APPROVALS_BACKLOG.md (approval queue debugging)
  - OUTBOX_STUCK.md (outbox job debugging)
  - Additional A2A incident runbooks

### Security & Compliance
- **Threat Model:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/security/THREAT_MODEL.md` for threat analysis (5 security pillars)
- **Evil Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/tests/evil_tests.md` for security validation (prompt injection, SQL injection, bypass attempts)

**⭐ Start Here:** Read `Trust-Spine-Package-Navigation.md` → then `PHASE_5_LAUNCH_GOVERNANCE/TEMPLATES/RELEASE_CHECKLIST.md` for production deployment.

---

## Learning Loop Implementation (NEW)

**Source:** `platform/CLAUDE_HANDOFF/03_LEARNING_LOOP.md`

The Learning Loop automates the incident-to-improvement cycle, capturing failures and converting them to skill pack enhancements.

### Learning Loop Architecture

```
Incident → Detection → Root Cause Analysis → Skill Update Proposal → Human Review → Deploy → Monitor
```

### Learning Loop Tasks

- [ ] **PHASE4-TASK-LL-001** Incident Detection Triggers
  - Monitor receipt failures (outcome = 'failed')
  - Monitor skill pack certification test failures
  - Monitor user escalations (authority queue denials > threshold)
  - Test: Failure triggers incident detection

- [ ] **PHASE4-TASK-LL-002** Root Cause Analysis Automation
  - Correlate failures with receipt history (correlation_id chain)
  - Identify common failure patterns (3+ similar failures)
  - Generate RCA report with recommendations
  - Test: RCA report accuracy (80% actionable recommendations)

- [ ] **PHASE4-TASK-LL-003** Skill Update Proposal Generation
  - Based on RCA, generate skill pack manifest update proposal
  - Propose permission adjustments, risk tier changes, new approval gates
  - Include test case additions for future prevention
  - Test: Proposal includes all necessary changes

- [ ] **PHASE4-TASK-LL-004** Human-in-the-Loop Review Gate
  - All skill updates require human approval (YELLOW tier)
  - Review dashboard for pending updates
  - One-click approve/reject with receipt logging
  - Test: Cannot deploy without approval

- [ ] **PHASE4-TASK-LL-005** Knowledge Graph Integration
  - Store learning patterns in Knowledge Graph
  - Enable cross-session pattern recognition
  - Prevent repeated similar incidents
  - Test: Pattern retrieval for known issues

### Learning Loop Success Criteria
- [ ] `4-SC-LL-001` Incident detection triggers within 5 minutes of failure
- [ ] `4-SC-LL-002` RCA reports generated for 100% of incidents
- [ ] `4-SC-LL-003` Human review gate enforced for all skill updates
- [ ] `4-SC-LL-004` Knowledge Graph stores 50+ learning patterns

---

## Quality Gate Workflow (NEW)

**Source:** `platform/brain/workflows/quality_gate.workflow.ts`

Automated QA workflow for skill pack certification before deployment.

### Quality Gate Stages

| Stage | Check | Fail Action |
|-------|-------|-------------|
| 1. Schema Validation | Manifest JSON valid | Block deploy |
| 2. Permission Audit | No over-permissioned tools | Block deploy |
| 3. Risk Tier Check | All RED tools have approval gates | Block deploy |
| 4. Certification Tests | All TC-* tests pass | Block deploy |
| 5. Receipt Coverage | 100% action coverage | Block deploy |
| 6. Security Scan | No secrets, PII patterns | Block deploy |

### Quality Gate Tasks

- [ ] **PHASE4-TASK-QG-001** Automated Manifest Validation
  - JSON schema validation for skill pack manifests
  - Required fields check (name, version, tools, permissions)
  - Test: Invalid manifest blocked at stage 1

- [ ] **PHASE4-TASK-QG-002** Permission Audit Automation
  - Scan manifest for over-permissioned tools
  - Flag tools with broader scope than needed
  - Test: Over-permissioned tool flagged

- [ ] **PHASE4-TASK-QG-003** Risk Tier Enforcement
  - All RED tier tools must have approval_required: true
  - YELLOW tier tools checked for appropriate gates
  - Test: RED tool without approval gate → blocked

- [ ] **PHASE4-TASK-QG-004** Certification Test Integration
  - Run all TC-* tests for skill pack
  - Collect test results and coverage
  - Generate certification report
  - Test: Failed TC-02 → deploy blocked

- [ ] **PHASE4-TASK-QG-005** Security Scanning
  - Scan for hardcoded secrets (API keys, passwords)
  - Scan for PII patterns (SSN, CC numbers)
  - Block deploy if secrets detected
  - Test: Hardcoded API key → blocked

### Quality Gate Success Criteria
- [ ] `4-SC-QG-001` All 13 skill packs pass Quality Gate workflow
- [ ] `4-SC-QG-002` Zero skill packs deployed without certification
- [ ] `4-SC-QG-003` Security scan catches 100% of test vulnerabilities

---

---

## Robots Framework v3.1 (NEW - Package 1 Integration)

**Source:** `plan/Aspire_Robots_Ops_v2_Council_Learning/robots/`

### Robot Framework Overview

Robots is a standalone toolkit for sync validation and smoke testing across 4 repositories:
- Admin Portal (`admin_portal_root`)
- Expo Mobile App (`expo_app_root`)
- Roadmap/Plan (`roadmap_root`)
- Trust Spine (`trustspine_root`)

### Robot Configuration

**Config File:** `robots/config/robots.config.yaml`
```yaml
mode: validate  # validate | smoke
paths:
  admin_portal_root: "./workspace/admin/zenith-operations-suite-main"
  expo_app_root: "./workspace/expo/Aspire-UI-Expokit"
  roadmap_root: "./workspace/roadmap/plan"
  trustspine_root: "./workspace/trustspine"
sentinels:
  admin_portal: ["package.json", "src/App.tsx"]
  expo_app: ["package.json", "app.json"]
  roadmap: ["Aspire-Production-Roadmap.md", "gates/gate-00-scope-lock.md"]
  trustspine: ["ADR/ADR-0001_Canonical_Identity_Model.md", ...]
```

### Robot Run Schema

**Output:** `robots/schemas/robot_run.schema.json`
```json
{
  "id": "string (min 8 chars)",
  "env": "staging | canary | production",
  "suite": "string",
  "status": "passed | failed | partial",
  "startedAt": "ISO8601",
  "finishedAt": "ISO8601",
  "versionRef": "string (git SHA)",
  "scenarios": [{
    "name": "string",
    "status": "passed | failed | partial",
    "summary": "string",
    "evidence": ["string[]"]
  }],
  "evidence": { "dir": "string", "artifacts": ["string[]"] }
}
```

### Robot Tasks

- [ ] **PHASE4-TASK-ROBOT-001** Deploy Robots Framework
  - Copy robots/ directory to CI/CD environment
  - Configure robots.config.yaml with correct paths
  - Run `scripts/verify_prereqs.sh` to validate setup
  - **Verification:** `python3 robots/run_suite.py --help` works

- [ ] **PHASE4-TASK-ROBOT-002** Configure GitHub Actions Workflow
  - Deploy `robots/workflows/github/robots.yml`
  - Configure workflow_dispatch with env input (staging|canary|production)
  - Setup evidence artifact upload to `robots/out/`
  - **Verification:** Manual workflow dispatch succeeds

- [ ] **PHASE4-TASK-ROBOT-003** Integrate with Learning Loop
  - Robot failures → `incident.opened` receipt
  - Robot passes → `robot.regression.completed` receipt
  - Wire `scripts/post_robot_run.py` to backend
  - **Verification:** Robot failure creates incident receipt

- [ ] **PHASE4-TASK-ROBOT-004** Validate 1000+ Scenarios
  - Create comprehensive test suite (1000+ scenarios)
  - Run full validation across all 4 repos
  - Achieve 95%+ pass rate baseline
  - **Verification:** Robot run report shows 1000+ scenarios

### Robots Success Criteria
- [ ] `4-SC-ROBOT-001` Robot framework deployed and configured
- [ ] `4-SC-ROBOT-002` GitHub Actions workflow operational
- [ ] `4-SC-ROBOT-003` Robot failures trigger incident receipts
- [ ] `4-SC-ROBOT-004` 1000+ scenarios in test suite

---

## 20 New Receipt Schemas (NEW - Package 2 Integration)

**Source:** `plan/Aspire_Ops_Receipts_Coverage_Addon_v1/platform/contracts/receipts/`

### Receipt Schema Categories

#### Deployment Receipts (5 schemas)
| Receipt Type | Required Fields | Purpose |
|--------------|-----------------|---------|
| `deploy.started` | release_id, environment, version | Deployment initiated |
| `deploy.canary.deployed` | release_id, environment, canary_percent, metrics_snapshot_ref | Canary at X% |
| `deploy.promoted` | release_id, environment, final_percent | Full rollout |
| `deploy.rolled_back` | release_id, environment, rollback_reason, previous_version | Rollback executed |
| `deploy.failed` | release_id, environment, error_code, error_message | Deployment failed |

#### SLO/Alert Receipts (3 schemas)
| Receipt Type | Required Fields | Purpose |
|--------------|-----------------|---------|
| `slo.breach.detected` | service, slo_name, window, threshold, observed, error_budget_remaining | SLO violation |
| `slo.metric.rollup` | service, slo_name, window, value, samples | Periodic SLO snapshot |
| `alert.triggered` | alert_name, severity, source, message | Alert fired |

#### Backup/DR Receipts (3 schemas)
| Receipt Type | Required Fields | Purpose |
|--------------|-----------------|---------|
| `backup.completed` | backup_id, target, size_bytes, duration_ms | Backup successful |
| `restore.tested` | backup_id, target, test_result, duration_ms | Restore test passed |
| `dr.drill.completed` | drill_id, scenario, participants, outcome | DR drill executed |

#### Entitlement Receipts (6 schemas)
| Receipt Type | Required Fields | Purpose |
|--------------|-----------------|---------|
| `entitlement.plan.changed` | suite_id, old_plan, new_plan, effective_date | Plan upgrade/downgrade |
| `entitlement.seat.added` | suite_id, office_id, seat_type | New seat provisioned |
| `entitlement.seat.removed` | suite_id, office_id, reason | Seat deprovisioned |
| `entitlement.grace.started` | suite_id, grace_type, grace_until | Grace period begins |
| `entitlement.grace.ended` | suite_id, grace_type, outcome | Grace period ends |
| `entitlement.usage.capped` | suite_id, resource, limit, current | Usage limit hit |

#### RBAC Receipts (3 schemas)
| Receipt Type | Required Fields | Purpose |
|--------------|-----------------|---------|
| `rbac.role.granted` | suite_id, target_office_id, role, granted_by | Role assigned |
| `rbac.role.revoked` | suite_id, target_office_id, role, revoked_by, reason | Role removed |
| `rbac.permission.escalated` | suite_id, office_id, permission, escalation_reason, approved_by | Temporary escalation |

### Receipt Schema Tasks

- [ ] **PHASE4-TASK-RCPT-001** Deploy Deployment Receipts
  - Add 5 deployment receipt schemas to platform contracts
  - Validate JSON Schema compliance
  - Wire to Control Plane rollout system
  - **Verification:** `deploy.canary.deployed` receipt generated on rollout

- [ ] **PHASE4-TASK-RCPT-002** Deploy SLO/Alert Receipts
  - Add 3 SLO/alert receipt schemas
  - Wire to Observability Stack
  - Configure breach detection → receipt generation
  - **Verification:** SLO breach creates `slo.breach.detected` receipt

- [ ] **PHASE4-TASK-RCPT-003** Deploy Backup/DR Receipts
  - Add 3 backup/DR receipt schemas
  - Wire to backup automation
  - Configure DR drill → receipt generation
  - **Verification:** Backup completion creates receipt

- [ ] **PHASE4-TASK-RCPT-004** Deploy Entitlement Receipts
  - Add 6 entitlement receipt schemas
  - Wire to billing/subscription system
  - Configure plan changes → receipt generation
  - **Verification:** Plan change creates `entitlement.plan.changed` receipt

- [ ] **PHASE4-TASK-RCPT-005** Deploy RBAC Receipts
  - Add 3 RBAC receipt schemas
  - Wire to permission management
  - Configure role changes → receipt generation
  - **Verification:** Role grant creates `rbac.role.granted` receipt

### Receipt Schema Success Criteria
- [ ] `4-SC-RCPT-001` All 20 receipt schemas deployed and validated
- [ ] `4-SC-RCPT-002` Deployment receipts flow through Control Plane
- [ ] `4-SC-RCPT-003` SLO breach receipts link to incidents
- [ ] `4-SC-RCPT-004` RBAC receipts capture approval chain

---

## High-Risk Approvals with Ava Video Enforcement (NEW - Package 1 Integration)

**Source:** `plan/Aspire_Robots_Ops_v2_Council_Learning/specs/SPEC_HighRisk_Approvals_AvaVideo_Gateway_TrustSpine.md`

### Server-Enforced Video Presence

**Database Schema (NEW):**
```sql
CREATE TABLE presence_sessions (
  id UUID PRIMARY KEY,
  suite_id UUID NOT NULL REFERENCES suites(id),
  office_id UUID NOT NULL REFERENCES offices(id),
  session_type TEXT NOT NULL CHECK (session_type IN ('ava_video', 'voice', 'text')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at TIMESTAMPTZ,
  client_fingerprint JSONB,
  CONSTRAINT active_session_unique UNIQUE (suite_id, office_id, session_type)
    WHERE ended_at IS NULL
);
```

### Gateway API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/sessions/ava/start` | POST | Start Ava video session |
| `/sessions/ava/heartbeat` | POST | Keep session alive (every 15s) |
| `/sessions/ava/end` | POST | End session |
| `/authority-items/{id}/approve` | POST | Approve with presence verification |

### Error Taxonomy

| Error Code | Description |
|------------|-------------|
| `VIDEO_REQUIRED` | Action requires active Ava video session |
| `PRESENCE_EXPIRED` | Session heartbeat timeout (>30s stale) |
| `EXECUTION_DISABLED` | System-wide execution pause |
| `SESSION_MISMATCH` | Session doesn't match requesting office |

### Actions Requiring Video Presence (RED Tier)

- Money transfers > $1000
- Payroll submission
- Contract signing
- Permission escalation
- Data deletion

### High-Risk Approval Tasks

- [ ] **PHASE4-TASK-HRA-001** Add presence_sessions Migration
  - Create migration for `presence_sessions` table
  - Add RLS policies (suite_id + office_id)
  - Add indexes for heartbeat queries
  - **Verification:** Migration runs successfully

- [ ] **PHASE4-TASK-HRA-002** Implement Gateway Endpoints
  - Add `/sessions/ava/start` endpoint
  - Add `/sessions/ava/heartbeat` endpoint
  - Add `/sessions/ava/end` endpoint
  - **Verification:** Session lifecycle works

- [ ] **PHASE4-TASK-HRA-003** Implement Presence Verification
  - Add presence check to authority item approval
  - Return `VIDEO_REQUIRED` error if no active session
  - Return `PRESENCE_EXPIRED` if heartbeat stale (>30s)
  - **Verification:** RED tier blocked without video

- [ ] **PHASE4-TASK-HRA-004** Wire to Mobile App
  - Mobile app calls `/sessions/ava/start` on video connect
  - Mobile app sends heartbeats every 15s
  - Mobile app calls `/sessions/ava/end` on disconnect
  - **Verification:** End-to-end video presence working

### High-Risk Approval Success Criteria
- [ ] `4-SC-HRA-001` presence_sessions table deployed with RLS
- [ ] `4-SC-HRA-002` Gateway endpoints operational
- [ ] `4-SC-HRA-003` RED tier actions blocked without video presence
- [ ] `4-SC-HRA-004` Mobile app maintains presence sessions

---

## Trust Spine Addons Integration (NEW)

**Source:** `platform/trust-spine/06_ADDONS/`

11 Trust Spine Addons require integration during Phase 4:

| Addon | Version | Purpose | Integration Phase |
|-------|---------|---------|-------------------|
| A2A_INBOX | V6 | Agent-to-agent task queue (7 migrations) | Phase 1 |
| AGENT_SYSTEM | V1 | Agent coordination framework | Phase 1 |
| BEHAVIOR_FLYWHEEL | V1 | Behavior learning + adaptation | **Phase 4** |
| CHECKLISTS_AND_TEMPLATES | V1 | Standard checklists for ops | Phase 0B |
| EXECUTOR_NODE_TS | V1 | TypeScript executor + observability | Phase 1 |
| LEGAL_DESK_CLARA | V1 | Clara legal skill pack workflows | Phase 2 |
| LLM_ROUTER | V1 | LLM routing policies | Phase 1 |
| NORA_CONFERENCE | V1 | Nora conference recording + transcription | Phase 2 |
| OBSERVABILITY_OTEL | V1 | OpenTelemetry instrumentation | **Phase 4** |
| REPLAY_HARNESS | V1 | Receipt replay + reconstruction | **Phase 4** |
| ops/runbooks/ | - | Operational runbooks (6) | **Phase 4** |

### Phase 4 Addon Integration Tasks

- [ ] **PHASE4-TASK-ADDON-001** BEHAVIOR_FLYWHEEL Integration
  - Deploy evaluation sets from `BEHAVIOR_FLYWHEEL_V1/`
  - Configure metric semantics
  - Wire to Learning Loop
  - Test: Behavior patterns tracked
  - **Verification:** Flywheel capturing behavior data

- [ ] **PHASE4-TASK-ADDON-002** OBSERVABILITY_OTEL Integration
  - Deploy from `OBSERVABILITY_OTEL_V1/`
  - Configure trace exporters
  - Configure metric collectors
  - Test: Traces visible in OTEL UI
  - **Verification:** Full observability stack operational

- [ ] **PHASE4-TASK-ADDON-003** REPLAY_HARNESS Integration
  - Deploy from `REPLAY_HARNESS_V1/`
  - Configure deterministic replay
  - Test: Can reconstruct state from receipts
  - Test: 100 receipts replay correctly
  - **Verification:** Replay demo working

- [ ] **PHASE4-TASK-ADDON-004** Operational Runbooks Deployment
  - Deploy 6 runbooks from `ops/runbooks/`:
    - `duplicate_transfer_prevention.md`
    - `gusto_token_refresh_failures.md`
    - `gusto_webhook_failures.md`
    - `payroll_submit_failed.md`
    - `provider_outage_mode.md`
    - `transfer_stuck_pending.md`
  - Test: Runbooks accessible and searchable
  - **Verification:** All runbooks deployed

- [ ] **PHASE4-TASK-ADDON-005** Additional Runbooks Deployment (NEW - P3 Gap Fix)
  - Deploy A2A runbooks from `06_ADDONS/A2A_INBOX_V6/runbooks/`:
    - `a2a_inbox.md` - A2A inbox operations
    - `a2a_incidents.md` - A2A incident handling
  - Deploy platform runbooks from `platform/runbooks/`:
    - `MAIL_OPS_OPERATIONS.md` - Mail operations procedures
    - `N8N_WORKFLOW_OPERATIONS.md` - n8n workflow operations
  - Deploy compliance runbooks from `compliance_pack/runbooks/`:
    - `MONEY_MOVEMENT_EMERGENCY_STOP.md` - Emergency stop for money movement
    - `RECEIPT_INTEGRITY_FAILURE.md` - Receipt integrity failure handling
    - `RECONCILIATION_DRIFT.md` - Reconciliation drift procedures
  - Test: All 7 additional runbooks accessible
  - **Verification:** Complete runbook library deployed (13 total)

---

## Evaluation Framework (NEW - P3 Gap Fix)

**Source:** `platform/brain/eval/`

The evaluation framework enables systematic testing of Brain Layer components.

### Eval Framework Structure

```
eval/
├── cases/                    [General eval cases]
├── conference/               [Conference-specific evals]
│   └── cases/
│       ├── case_01_email_followup.json
│       ├── case_02_talk_turns.json
│       └── case_03_high_risk_interrupt.json
├── council/                  [Council eval fixtures]
│   └── fixtures/
│       └── example_incident.json
├── learning/                 [Learning eval cases]
├── README.md
└── run_eval.ts              [Eval runner]
```

### Eval Framework Tasks

- [ ] **PHASE4-TASK-EVAL-001** Deploy Eval Runner
  - Deploy `brain/eval/run_eval.ts`
  - Configure test case discovery
  - Setup evaluation result storage
  - Test: Eval runner executes test cases
  - **Verification:** Eval runner operational

- [ ] **PHASE4-TASK-EVAL-002** Deploy General Eval Cases
  - Deploy cases from `brain/eval/cases/`
  - Configure expected outputs
  - Setup pass/fail thresholds
  - Test: General cases pass baseline
  - **Verification:** General eval cases deployed

- [ ] **PHASE4-TASK-EVAL-003** Deploy Conference Eval Cases
  - Deploy from `brain/eval/conference/cases/`:
    - `case_01_email_followup.json` - Email follow-up after meeting
    - `case_02_talk_turns.json` - Speaker turn handling
    - `case_03_high_risk_interrupt.json` - High-risk interruption handling
  - Test: Conference cases validate Nora behavior
  - **Verification:** Conference eval cases deployed

- [ ] **PHASE4-TASK-EVAL-004** Deploy Council Eval Fixtures
  - Deploy from `brain/eval/council/fixtures/`:
    - `example_incident.json` - Example incident for council evaluation
  - Test: Council deliberation can use fixtures
  - **Verification:** Council fixtures deployed

- [ ] **PHASE4-TASK-EVAL-005** Deploy Learning Eval Cases
  - Deploy from `brain/eval/learning/`
  - Configure learning loop validation
  - Test: Learning improvements measurable
  - **Verification:** Learning eval cases deployed

### Eval Framework Success Criteria
- [ ] `4-SC-EVAL-001` Eval runner can execute all test cases
- [ ] `4-SC-EVAL-002` Conference cases pass for Nora
- [ ] `4-SC-EVAL-003` Council fixtures validate deliberation
- [ ] `4-SC-EVAL-004` Learning cases track improvement over time

---

## Observability Stack Implementation (NEW)

**Sources:**
- `OBSERVABILITY_DEFAULTS.md`
- `platform/trust-spine/06_ADDONS/OBSERVABILITY_OTEL_V1/`
- `platform/trust-spine/06_ADDONS/EXECUTOR_NODE_TS_V1/observability/`

### Observability Tasks

- [ ] **PHASE4-TASK-OBS-001** Deploy OpenTelemetry Instrumentation
  - Instrument all services with OTEL SDK
  - Configure trace context propagation
  - Configure correlation ID flow
  - Test: Traces span all services
  - **Verification:** End-to-end tracing working

- [ ] **PHASE4-TASK-OBS-002** Configure Alerting Rules
  - Deploy alerting rules for:
    - Error rate > threshold
    - Latency p95 > SLO
    - Receipt coverage < 100%
    - RLS violations detected
  - Test: Alerts fire correctly
  - **Verification:** Alerting operational

- [ ] **PHASE4-TASK-OBS-003** Setup Grafana Dashboards
  - Deploy dashboards from `platform/observability/dashboards/`:
    - Receipt coverage dashboard
    - Skill pack performance dashboard
    - Provider health dashboard
    - SLO compliance dashboard
  - Test: Dashboards show live data
  - **Verification:** All dashboards operational

- [ ] **PHASE4-TASK-OBS-004** Define SLI/SLO Metrics (ADR-0007)
  - Implement SLI definitions from ADR-0007:
    - Latency: p95 < 800ms
    - Error rate: < 1%
    - Receipt coverage: 100%
    - Tool success rate: > 99%
  - Configure SLO tracking
  - Test: SLO violations detected
  - **Verification:** SLO monitoring active

### Observability Success Criteria
- [ ] `4-SC-OBS-001` All services instrumented with OTEL
- [ ] `4-SC-OBS-002` Alerting rules configured and tested
- [ ] `4-SC-OBS-003` Grafana dashboards showing live data
- [ ] `4-SC-OBS-004` SLO compliance tracking operational

---

## Internal Skill Packs (Admin/Ops) - NEW

**Source:** `platform/control-plane/registry/skillpacks.internal.json`

4 internal skill packs for admin/operations (NOT user-facing):

| ID | Owner | Profile | Purpose |
|----|-------|---------|---------|
| `sre_triage` | admin | internal_ops | SRE incident triage automation |
| `qa_evals` | admin | internal_ops | QA evaluation runs |
| `security_review` | admin | internal_security | Security review automation |
| `release_manager` | admin | internal_ops | Release management automation |

### Internal Skill Pack Deployment

- [ ] **PHASE4-TASK-ISP-001** SRE Triage Skill Pack
  - Deploy `sre_triage` internal skill pack
  - Configure incident detection rules
  - Wire to PagerDuty/Opsgenie
  - Test: Incident triggers triage workflow
  - **Verification:** SRE triage operational

- [ ] **PHASE4-TASK-ISP-002** QA Evals Skill Pack
  - Deploy `qa_evals` internal skill pack
  - Configure eval suite execution
  - Wire to CI/CD pipeline
  - Test: Evals run on PR merge
  - **Verification:** QA evals automated

- [ ] **PHASE4-TASK-ISP-003** Security Review Skill Pack
  - Deploy `security_review` internal skill pack
  - Configure security scan rules
  - Wire to GitHub Actions
  - Test: Security review on sensitive changes
  - **Verification:** Security review automated

- [ ] **PHASE4-TASK-ISP-004** Release Manager Skill Pack
  - Deploy `release_manager` internal skill pack
  - Configure release checklist automation
  - Wire to deployment pipeline
  - Test: Release checklist enforced
  - **Verification:** Release management automated

### Internal Skill Pack Certification

**Same TC-01, TC-02, TC-03 requirements as external skill packs:**
- [ ] TC-01: Bounded Authority (internal skill packs)
- [ ] TC-02: Receipt Integrity (internal skill packs)
- [ ] TC-03: PII Redaction (internal skill packs)

### Internal Skill Pack Success Criteria
- [ ] `4-SC-ISP-001` All 4 internal skill packs deployed
- [ ] `4-SC-ISP-002` All internal skill packs pass TC-01/02/03
- [ ] `4-SC-ISP-003` SRE triage automation working
- [ ] `4-SC-ISP-004` Release management automation working

---

## Mail/Domains Production Gate Checklist (NEW — Phase 0C/2 Verification)

**All mail/domain features must pass these gates before production readiness.**

### Gate 1: Testing (Mail)
- [ ] RLS isolation: Suite A cannot read Suite B's mail_domains/mail_accounts
- [ ] Evil test: Cross-tenant domain claim denied
- [ ] Evil test: Expired/wrong-scope capability token rejected on domain operations
- [ ] Evil test: Fake approval_id rejected on domain purchase (RED tier)
- [ ] State machine: All 13 BYOD onboarding states reachable
- [ ] State machine: All 13 Buy Domain states reachable
- [ ] Provider adapters: EmailArray + ResellerClub handle 4xx/5xx with retry/backoff
- [ ] Receipt coverage: 100% of mail state-changing operations produce receipts (16 types)
- [ ] Code coverage >= 80% for mail skill pack + Domain Rail

### Gate 2: Observability (Mail)
- [ ] Correlation IDs flow: Desktop/Mobile → orchestrator → Domain Rail → ResellerClub
- [ ] `provider_call_log` entries for ALL ResellerClub + EmailArray API calls
- [ ] Domain Rail `/health` endpoint (liveness + readiness)
- [ ] SLO: Domain Rail p95 < 2s

### Gate 3: Reliability (Mail)
- [ ] Circuit breaker on ResellerClub client
- [ ] Circuit breaker on EmailArray client
- [ ] Idempotency keys for domain purchase (no duplicate registrations)
- [ ] Timeout: Domain Rail → ResellerClub < 10s
- [ ] Dead letter queue: failed mail ops → `outbox_dead_letters`

### Gate 4: Operations (Mail)
- [ ] 6 runbooks ported from handoff package (mail-specific)
- [ ] Rollback: Domain Rail revertible in < 5 min
- [ ] Incident escalation workflow (n8n) for mail delivery failures

### Gate 5: Security (Mail)
- [ ] No API keys in git (Railway env vars only — ResellerClub, EmailArray, S2S secrets)
- [ ] S2S auth between orchestrator and Domain Rail enforced
- [ ] PII redaction in mail receipts (email addresses → `<EMAIL_REDACTED>` unless business email)
- [ ] Domain Rail not publicly reachable without S2S auth

---

## 11 Unified Production Gates

### Trust Spine Gates (ADR-0008) - 8 Gates

#### Gate 1: Schema Locked
- [ ] **PHASE4-GATE-001** Verify Schema Stability
  - No breaking schema changes after Phase 0B deployment
  - All migrations backward-compatible
  - Version field in migrations table
  - **Validation:** Schema diff check (current vs. production)

#### Gate 2: RLS Policies Enforced
- [ ] **PHASE4-GATE-002** Verify Tenant Isolation
  - RLS enabled on ALL tenant-scoped tables
  - Zero cross-tenant leakage (verified by `rls_isolation_test.py`)
  - Policy audit log (which policies active on which tables)
  - **Validation:** `SELECT * FROM pg_policies` → verify coverage

#### Gate 3: Receipt Coverage 100%
- [ ] **PHASE4-GATE-003** Verify Receipt Generation
  - ALL state-changing operations generate receipts
  - Audit: scan codebase for DB writes → verify receipt creation
  - Coverage report: actions vs receipts (target: 100%)
  - **Validation:** `receipt_coverage_test.py` passes

#### Gate 4: Hash Chain Verified
- [ ] **PHASE4-GATE-004** Verify Hash Chain Integrity
  - No broken links in receipt hash chain
  - Verification service returns VALID status
  - Weekly hash chain verification job (automated)
  - **Validation:** `POST /v1/receipts/verify-run` → VALID

#### Gate 5: Outbox Idempotency Proven
- [ ] **PHASE4-GATE-005** Verify Idempotency
  - Duplicate idempotency_key returns cached receipt
  - Concurrency test passes (no duplicate claims)
  - DLQ captures permanent failures
  - **Validation:** `idempotency_test.py` + `outbox_concurrency_test.py` pass

#### Gate 6: Policy Evaluation Accurate
- [ ] **PHASE4-GATE-006** Verify Policy Decisions
  - ALLOW/DENY/REQUIRE_APPROVAL decisions correct
  - Risk tier mapping validated (low/medium/high → Green/Yellow/Red)
  - Policy audit log (which decisions made for which actions)
  - **Validation:** `policy_eval_test.py` passes

#### Gate 7: E2E Tests Passing
- [ ] **PHASE4-GATE-007** Verify End-to-End Flow
  - OpenAPI contract validated (Postman collection all green)
  - Receipt creation → outbox execution → receipt update flow works
  - Approval workflow tested (REQUIRE_APPROVAL → user approves → execute)
  - **Validation:** `tests/substrate/e2e_test.py` passes

#### Gate 8: Load Tests Passing
- [ ] **PHASE4-GATE-008** Verify Performance Under Load
  - k6 stress test: 1000 req/s sustained for 5 minutes
  - p95 latency <500ms under load
  - Zero errors during stress test
  - **Validation:** `k6 run stress_tests/receipts_load_test.js` → all green

### Aspire Gates - 3 Gates

#### Gate 9: Safety Gateway Effective
- [ ] **PHASE4-GATE-009** Verify Safety Systems
  - NeMo Guardrails blocks jailbreak attempts (test suite passes)
  - Presidio DLP redacts all PII (no SSN/CC/email in receipts)
  - Safety bypass attempts logged and blocked
  - **Validation:** `safety_gateway_test.py` passes

#### Gate 10: Capability Tokens Enforced
- [ ] **PHASE4-GATE-010** Verify Token Security
  - Invalid tokens rejected with 403 Forbidden
  - Expired tokens rejected (<60s TTL enforced)
  - Token signature verification server-side
  - **Validation:** `capability_token_test.py` passes

#### Gate 11: LangGraph Orchestrator Stable
- [ ] **PHASE4-GATE-011** Verify Orchestrator Reliability
  - Correlation IDs flow end-to-end (intake → receipt)
  - Orchestrator error rate <1% over 24h soak test
  - Single Brain Authority maintained (no autonomous tool decisions)
  - **Validation:** `orchestrator_test.py` + 24h soak test pass

---

## Success Criteria

**ALL 11 GATES MUST PASS.** No exceptions. No "we'll fix it later."

- [ ] `4-SC-001` All 11 production gates pass
- [ ] `4-SC-002` 10/10 Bundle generated (Gate Reports + Evidence + Runbooks)
- [ ] `4-SC-003` Production deployment approved

---

## Related Artifacts

**Created in This Phase:**
- Gate validation reports (11 reports)
- Evidence bundles (test results, screenshots, metrics)
- Production runbooks (deployment, incident response, rollback)
- Security review checklist
- Performance benchmarks

**Used in Later Phases:**
- Phase 5: Production deployment procedures
- Phase 6: Scaling strategies based on load test results

---

## Estimated Duration

**Full-time:** Approximately 10 weeks (Week 25-35)

**Timeline:**
- Week 25-27: Trust Spine Gates 1-4 (Schema, RLS, Receipts, Hash Chain)
- Week 28-30: Trust Spine Gates 5-8 (Idempotency, Policy, E2E, Load Tests)
- Week 31-32: Aspire Gates 9-11 (Safety Gateway, Tokens, Orchestrator)
- Week 33-34: 10/10 Bundle generation + documentation
- Week 35: Final production readiness review

**Buffer:** 2 weeks built into timeline for remediation

---

## Cost

**$40-60/mo** - Sentry (error tracking), load testing tools, security scanning

---

## Notes

**Gate Interdependencies:**
- Gate 1 (Schema Locked) must pass before Gate 2 (RLS Policies)
- Gate 3 (Receipt Coverage) enables Gate 4 (Hash Chain Verification)
- Gate 6 (Policy Evaluation) required for Gate 7 (E2E Tests)
- All gates must pass before production deployment

**Blocking Notes:**
- Phase 5 (Beta Launch) cannot start until all 11 gates pass
- No shortcuts allowed - production readiness is non-negotiable

---

## Related Files

- **Main Roadmap:** [Aspire-Production-Roadmap.md](../Aspire-Production-Roadmap.md)
- **Success Criteria:** [00-success-criteria-index.md](../00-success-criteria-index.md)
- **Previous Phase:** [phase-3-mobile-app.md](phase-3-mobile-app.md)
- **Next Phase:** [phase-5-beta-launch.md](phase-5-beta-launch.md)

---

**Last Updated:** 2026-02-08
**Status:** ⏳ NOT STARTED (waiting for Phase 3 completion)
