# Deep Scan v3 - Findings Archive

**Date:** 2026-02-07
**Scope:** 2,045+ files across plan/, CLAUDE.md, and zip packages
**Sync Score:** 6.5/10 (before Foundation Sync)

---

## Summary

The deep scan identified 20 conflicts across the Aspire codebase and plan documentation. All 20 conflicts have been resolved through the Phase 0 Foundation Sync.

**Resolution tracking:** See [registries/conflict-registry.yaml](registries/conflict-registry.yaml)

---

## Findings by Category

### Critical (5)
- **C1:** Risk tier naming (58 files use low/medium/high vs 12 correct green/yellow/red) -> Resolved in risk-tiers.enum.yaml
- **C2:** Receipt table naming (execution_receipts vs receipts) -> Resolved in receipts.schema.v1.yaml
- **C3:** Tenant identity (suite_id vs tenant_id) -> Resolved in tenant-identity.yaml
- **C4:** Receipt field count mismatch (47 cols vs 15 mandatory) -> Resolved in receipts.schema.v1.yaml
- **C5:** correlation_id vs trace_id -> Resolved in receipts.schema.v1.yaml

### High (8)
- **H1:** Approval status (denied vs rejected) -> Resolved in approval-status.enum.yaml
- **H2:** Outcome status normalization -> Resolved in outcome-status.enum.yaml
- **H3:** Gate system conflict (5 categories vs 11 checkpoints) -> Resolved in gate-reconciliation.md
- **H4:** Phase 0A status contradiction -> Fixed in lean roadmap v5.0
- **H5:** Phase A-F unmapped to 0A-6 -> Resolved in phase-mapping.yaml
- **R1:** Roadmap is 2,000+ line monolith -> Rewritten to 189 lines
- **R6:** Intent Ingest API uses low/medium/high -> Fixed in ecosystem-architecture.md
- **R7:** Canonical Receipt Object incomplete -> Schema is source of truth

### Medium (7)
- **M1:** Skill pack names unmapped -> Resolved in skill-pack-registry.yaml
- **M2:** Skill pack count inconsistency -> Registry has exact count (15)
- **R8:** Gate 5 references High/Medium/Low -> Fixed in lean roadmap
- **R16:** Phase 2 skill pack count mismatch -> Registry clarifies
- **R17:** Migration counts inconsistent -> Resolved (all references now point to CANONICAL_PATHS.md for canonical counts)

---

## Structural Issues Resolved

| Issue | Before | After |
|-------|--------|-------|
| Roadmap size | 2,466 lines | 189 lines |
| Schema files | 2 | 6 (canonical) |
| Registry files | 2 | 4 |
| Reference docs | 0 (embedded in monolith) | 5 (extracted) |
| Phase files | 9 | 10 (added Phase 0) |
| Gate reconciliation | None | Documented |
| Phase mapping | None | Complete A-F to 0A-6 |
| Conflict tracking | None | 20/20 tracked |

---

## Post-Sync Score

After Foundation Sync completion, the expected sync score improves from 6.5/10 to ~9/10. Remaining work (actual code migration of 58 files from low/medium/high to green/yellow/red) happens during Phase 1 implementation.

---

**End of Deep Scan v3 Archive**
