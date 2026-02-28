# CANONICAL_PATHS.md
> Maps plan concepts to exact ecosystem file/directory locations.
> All paths are relative to project root.
> Last Updated: 2026-02-08

## Variable Definition

```
ECOSYSTEM_ROOT = plan/temp_ecosystem_scan/aspire_ecosystem_v12.7_2026-02-03/
```

## Trust Spine Artifacts

| Concept | Canonical Ecosystem Path | Count |
|---------|-------------------------|-------|
| Trust Spine Core Migrations | `${ECOSYSTEM_ROOT}/platform/trust-spine/03_SUPABASE_MIGRATIONS_ADDON/migrations/` | 42 files |
| Migration Order Doc | `${ECOSYSTEM_ROOT}/platform/trust-spine/03_SUPABASE_MIGRATIONS_ADDON/MIGRATION_ORDER_ADDON.md` | 1 file |
| A2A Migrations | `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/A2A_INBOX_V6/02_DB/migrations/` | 7 files |
| Core Edge Functions | `${ECOSYSTEM_ROOT}/platform/trust-spine/04_EDGE_FUNCTIONS/supabase/functions/` | 5 (approval-events, inbox, outbox-executor, outbox-worker, policy-eval) |
| A2A Edge Functions | `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/A2A_INBOX_V6/05_PLATFORM/supabase/functions/` | 3 (a2a-inbox-claim, a2a-inbox-enqueue, a2a-inbox-transition) |
| Go Receipt Verifier | `${ECOSYSTEM_ROOT}/platform/trust-spine/01_ORIGINAL_INPUTS/claude_handoff_4_0/phase0_bootstrap/aspire_claude_bootstrap/internal/receiptsverifier/` | 1 package |
| Skill Pack Manifest Schema | `${ECOSYSTEM_ROOT}/platform/trust-spine/12_CERTIFICATION/pack_manifest.schema.json` | 1 file |
| Skill Pack Manifest Template | `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_3_SKILL_PACKS_CERTIFICATION/TEMPLATES/PACK_MANIFEST_TEMPLATE.json` | 1 file |
| E2E Tests (numbered) | `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/sql/` | 5 files (01-05) |
| Stress Tests | `${ECOSYSTEM_ROOT}/platform/trust-spine/14_STRESS_TESTS/` | k6 + pgbench |
| ADRs | `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/` | 8 ADRs (0001-0008) |
| Job Kit Phase 1 Runbook | `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_1_TRUST_SPINE/RUNBOOKS/PHASE_1_APPLY_DEPLOY_TEST.md` | 1 file |

## Platform Artifacts

| Concept | Canonical Ecosystem Path |
|---------|-------------------------|
| Agent Personas | `${ECOSYSTEM_ROOT}/agent_kits/` |
| Skill Pack Manifests | `${ECOSYSTEM_ROOT}/skillpacks/` |
| Brain State Machines | `${ECOSYSTEM_ROOT}/platform/brain/state_machines/` |
| Gateway Policies | `${ECOSYSTEM_ROOT}/platform/gateway/policies/` |
| Gateway Tools Catalog | `${ECOSYSTEM_ROOT}/platform/gateway/policies/tools_catalog.yaml` |
| Control Plane | `${ECOSYSTEM_ROOT}/platform/control-plane/` |
| n8n Workflows | `${ECOSYSTEM_ROOT}/platform/integrations/n8n/workflows/` |
| CANONICAL_MAP | `${ECOSYSTEM_ROOT}/CANONICAL_MAP.md` |

## Job Kit Phase Name Mapping

| Plan Phase | Job Kit Folder Name |
|------------|-------------------|
| Phase 0A | PHASE_0A_FOUNDATIONS |
| Phase 0B | PHASE_0B_DEVOPS_BASE |
| Phase 1 | PHASE_1_TRUST_SPINE |
| Phase 2 | PHASE_2_FEATURE_TEMPLATE |
| Phase 2 (Skill Packs) | PHASE_3_SKILL_PACKS_CERTIFICATION |
| Phase 4 | PHASE_4_RELIABILITY_SCALE |
| Phase 5 | PHASE_5_LAUNCH_GOVERNANCE |
| Phase 6 | PHASE_6_POST_LAUNCH_OPERATIONS |

## Stale References (DO NOT USE)

These references appeared in plan files before this sync and are **incorrect**:

| Stale Reference | Correct Replacement |
|----------------|-------------------|
| `Trust Spine Package/` | `${ECOSYSTEM_ROOT}/platform/trust-spine/` |
| `PHASE_2_SUBSTRATE_VALIDATION` | `PHASE_2_FEATURE_TEMPLATE` |
| `PHASE_3_INTELLIGENCE_INTEGRATION` | `PHASE_3_SKILL_PACKS_CERTIFICATION` |
| `PHASE_4_PROVIDER_INTEGRATIONS` | `PHASE_4_RELIABILITY_SCALE` |
| `PHASE_6_MOBILE_INTEGRATION` | `PHASE_6_POST_LAUNCH_OPERATIONS` |
| `PHASE_7_PRODUCTION_OPERATIONS` | Does not exist in ecosystem. Use `PHASE_6_POST_LAUNCH_OPERATIONS` |
| `PHASE_8_SCALE` | Does not exist in ecosystem. Use `PHASE_6_POST_LAUNCH_OPERATIONS` |
| `ADR-0001-suite-office-identity.md` | `ADR-0001-canonical-identity.md` |
| `ADR-0008-release-gates.md` | `ADR-0008-release-governance.md` |
| `A2A_INBOX_V6/go/` | No `go/` subdirectory exists. Go code lives in `01_ORIGINAL_INPUTS/` |
| "109 migrations (86 base + 16 Trust Spine + 7 A2A)" | See DEPENDENCIES.lock.md for canonical counts |
