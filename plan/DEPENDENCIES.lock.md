# DEPENDENCIES.lock.md
> Pinned ecosystem version and canonical source references.
> Last Updated: 2026-02-08

## Ecosystem Version

| Field | Value |
|-------|-------|
| **Ecosystem Version** | `aspire_ecosystem_v12.7_2026-02-03` |
| **Location** | `plan/temp_ecosystem_scan/aspire_ecosystem_v12.7_2026-02-03/` |
| **Pinned Date** | 2026-02-08 |
| **Variable** | `${ECOSYSTEM_ROOT}` |

## Canonical Sources

All plan references MUST resolve against these canonical paths.

| Concept | Canonical Path |
|---------|---------------|
| CANONICAL_MAP | `${ECOSYSTEM_ROOT}/CANONICAL_MAP.md` |
| Trust Spine Migrations | `${ECOSYSTEM_ROOT}/platform/trust-spine/03_SUPABASE_MIGRATIONS_ADDON/` |
| Migration Order | `${ECOSYSTEM_ROOT}/platform/trust-spine/03_SUPABASE_MIGRATIONS_ADDON/MIGRATION_ORDER_ADDON.md` |
| A2A Addon | `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/A2A_INBOX_V6/` |
| Edge Functions | `${ECOSYSTEM_ROOT}/platform/trust-spine/04_EDGE_FUNCTIONS/supabase/functions/` |
| Certification Schema | `${ECOSYSTEM_ROOT}/platform/trust-spine/12_CERTIFICATION/pack_manifest.schema.json` |
| E2E Tests | `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/sql/` |
| Stress Tests | `${ECOSYSTEM_ROOT}/platform/trust-spine/14_STRESS_TESTS/` |
| ADRs | `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/` |
| Job Kit Runbooks | `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_1_TRUST_SPINE/RUNBOOKS/` |

## Validation

Run `python plan/tools/validate_plan_refs.py` to verify all references resolve.
Run `python plan/tools/validate_consistency.py` to verify cross-file consistency.
