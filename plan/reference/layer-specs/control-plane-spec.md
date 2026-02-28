# Control Plane Layer Specification

**Purpose:** Governs changes to policies, prompts, tool schemas, and model routing using the Trust Spine pattern (proposal → approval → outbox → execution → receipts).
**Build Phase:** 2 (Founder MVP)
**Readiness:** Scaffolded (40%) — DB migrations, OpenAPI spec, docs, runbooks exist. No deployment machinery.
**Ecosystem Path:** `platform/control-plane/`
**File Count:** 75 files

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    CONTROL PLANE                              │
│                                                              │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────┐       │
│  │ Registry │  │ Change       │  │ Rollout Engine    │       │
│  │          │→ │ Control      │→ │                    │       │
│  │ policy   │  │ (proposal →  │  │ 0% → 1% → 10%    │       │
│  │ prompt   │  │  approval →  │  │ → 50% → 100%      │       │
│  │ tool     │  │  receipt)    │  │                    │       │
│  │ model    │  │              │  │ Rollback: always   │       │
│  └──────────┘  └──────────────┘  └──────────────────┘       │
│       ↓                                                      │
│  ┌──────────────────────────────────────────────────┐        │
│  │ Executor (idempotent apply/rollback)             │        │
│  │ publish / activate / rollback config changes     │        │
│  └──────────────────────────────────────────────────┘        │
└──────────────────────────────────────────────────────────────┘
```

**Key Principle:** "Personality never grants capability." Permissions are enforced by tool allowlists in runtime + registry-controlled rules.

---

## Component Inventory

### 1. Registry

**Components:** Stores versioned artifacts across 4 types:

| Artifact Type | What's Stored | Example |
|--------------|--------------|---------|
| **Policy** | Gateway enforcement rules, risk tier definitions | `finn_capabilities.yaml` v2 |
| **Prompt** | Agent system prompts, shared governance blocks | `adam_research/system_prompt.md` v3 |
| **Tool Schema** | Tool definitions, capability scopes | `tools_catalog.yaml` v1 |
| **Model Policy** | LLM router rules, per-agent routing | `router_policy.yaml` v1 |

### 2. State Machine Lifecycle

**Source:** `docs/02_state_machine.md`

```
draft → staged → active → deprecated → archived
```

| Transition | Requirements |
|-----------|-------------|
| draft → staged | Review ready (can be validated) |
| staged → active | **Requires approval receipt** (Trust Spine integration) |
| active → deprecated | **Requires approval receipt** |
| deprecated → archived | Optional, internal cleanup |

**Rollback:** Active → previous active version. Always allowed WITH approval.

### 3. Canary Rollout Strategy

**Source:** `docs/03_rollout_and_canary.md`

```
0% → 1% → 10% → 50% → 100%
```

Traffic exposure is controlled separately from lifecycle state. A registry item can be `active` but at `1%` rollout.

**V1 Simplification (Recommended):** Binary rollout (active/inactive) for single-tenant dogfood. Keep canary infrastructure for v2.

### 4. DB Migrations (8 files)

**Zip Path:** `platform/control-plane/db/migrations/`

| Migration | Tables Created |
|-----------|---------------|
| `0001_control_plane_tables.sql` | `registry_items`, `rollouts`, `suite_config_versions`, `config_change_events` |
| `0002_pgvector.sql` | Vector extension support for semantic search |
| `0003_learning_objects.sql` | Learning system data storage |
| `0004_eval_runs.sql` | Eval harness results tracking |
| `0005_incident_signatures.sql` | Incident pattern matching |
| `0006_legal_desk_contracts.sql` | Contract management (Clara) |
| `0007_pandadoc_usage_metering.sql` | Legal document billing |
| `0008_meeting_sessions.sql` | Conference session tracking |

### 5. OpenAPI Specification

**Zip Path:** `platform/control-plane/api/openapi.yaml`

**Endpoints:**
- `GET /registry` — List registry items (filtered by type, state)
- `POST /registry` — Create new registry item (draft state)
- `POST /registry/{id}/transition` — State transition (requires approval for active)
- `POST /rollouts` — Create rollout for a registry item
- `PUT /rollouts/{id}/percentage` — Set rollout percentage

### 6. Permission Policies

**Zip Path:** `platform/control-plane/policies/`

| Policy | Purpose |
|--------|---------|
| `permissions.external.yaml` | What external parties (clients, partners) can access |
| `permissions.internal.yaml` | What internal agents/services can access |
| `permissions.legal.overlay.yaml` | Legal-specific permission overrides (Clara) |
| `conference_defaults.yaml` | Default conference room settings |

### 7. Provider Control Center

**Zip Path:** `platform/control-plane/provider-control-center/`

| File | Purpose |
|------|---------|
| `gusto_connection.schema.json` | Gusto OAuth connection configuration |
| `gusto_health_checks.md` | Health monitoring for Gusto integration |
| `money_movement_health.md` | Transfer system health monitoring |

### 8. Operational Runbooks (7)

**Zip Path:** `platform/runbooks/`

| Runbook | Trigger | Resolution |
|---------|---------|------------|
| `duplicate_transfer_prevention.md` | Duplicate transfer detected | Idempotency check + cancel |
| `gusto_token_refresh_failures.md` | OAuth token refresh fails | Re-auth flow + credential rotation |
| `gusto_webhook_failures.md` | Webhook delivery failures | Retry + manual verification |
| `payroll_submit_failed.md` | Payroll submission fails | Verify data, retry or escalate |
| `provider_outage_mode.md` | Provider API down | Circuit breaker + graceful degradation |
| `transfer_stuck_pending.md` | Transfer stuck in pending state | Diagnose, manual resolution |
| `N8N_WORKFLOW_OPERATIONS.md` | n8n workflow issues | Debug, restart, escalate |

### 9. Skill Pack Certification (TC-01/02/03)

**Source:** `plan/registries/skill-pack-registry.yaml` + `platform/trust-spine/12_CERTIFICATION/`

| Test Code | Name | What It Validates |
|-----------|------|------------------|
| TC-01 | Bounded Authority | Agent stays within tool allowlist, cannot escalate privileges |
| TC-02 | Receipt Integrity | Every action produces valid receipt with all mandatory fields |
| TC-03 | PII Redaction | No PII leaks in logs, receipts, or API responses |

**Status:** Defined but not implemented. Implementation is Phase 4 (Hardening).

---

## Trust Spine Integration

The Control Plane uses the same governance pattern as all other layers:

```
Config Change Proposal → Trust Spine Approval → Outbox → Executor → Receipt
```

Every registry state transition generates:
1. A proposal (stored in Trust Spine inbox)
2. An approval event (for staged → active transitions)
3. A receipt (documenting the change + who approved it)

**Handoff doc:** `CLAUDE_HANDOFF_README.md`

---

## Implementation Readiness: 40%

| Component | Status | What's Needed |
|-----------|--------|---------------|
| DB Migrations | **Implemented** | Apply to Supabase |
| OpenAPI Spec | **Designed** | Implement endpoints |
| State Machine | **Designed** | Implement lifecycle transitions |
| Canary Rollout | **Designed** | Binary for v1, canary for v2 |
| Permission Policies | **Designed** | Load and enforce at runtime |
| Runbooks | **Designed** | Ready for operations |
| Certification | **Designed** | Implement TC-01/02/03 tests (Phase 4) |
| Executor | **Not Started** | Implement idempotent config apply |

---

**End of Control Plane Layer Specification**
