# Ava Enterprise Sync Manifest

**Sync date:** 2026-02-12
**Synced by:** Claude Code (Aspire Co-Founder Engineer)
**Packages:**
- Ava Admin Enterprise Handoff v2 (40 files, generated 2026-02-12)
- Ava User Enterprise Handoff v1.1 (31 files)

---

## Source Integrity

### Ava Admin v2
- Source: `_ava-scan/ava-admin-enterprise-handoff-v2.zip`
- Extracted to: `_ava-scan/ava-admin/`
- Checksum file: `_ava-scan/ava-admin/CHECKSUMS.sha256`

### Ava User v1.1
- Source: `_ava-scan/ava-user-enterprise-handoff_v1.1.zip`
- Extracted to: `_ava-scan/ava-user/ava-user-enterprise-handoff_v1.1/`

---

## File Placement Map

### plan/contracts/ava-admin/ (4 files)
| File | Source | Notes |
|------|--------|-------|
| change_proposal.schema.json | ava-admin/contracts/ | Clean copy |
| incident_packet.schema.json | ava-admin/contracts/ | Clean copy |
| ops_exception_card.schema.json | ava-admin/contracts/ | Clean copy |
| ops_telemetry_facade.openapi.yaml | ava-admin/contracts/ | Clean copy |

### plan/contracts/ava-user/ (2 files)
| File | Source | Notes |
|------|--------|-------|
| ava_orchestrator_request.schema.json | ava-user/contracts/ | Clean copy |
| ava_result.schema.json | ava-user/contracts/ | **NORMALIZED: risk tier enum changed from UPPERCASE to lowercase** |

### plan/specs/ava-admin/ (6 files)
| File | Source(s) | Notes |
|------|-----------|-------|
| receipt_chain_spec.md | ava-admin/runtime/ | Added cross-references |
| dlp_redaction_matrix.md | ava-admin/security/ | Added cross-references |
| operator_engineer_toggle.md | ava-admin/policies/ | Added cross-references |
| learning_loop_spec.md | ava-admin/learning_loop/ (2 files merged) | Combined prevention_pipeline + robots_integration |
| incident_ops_spec.md | ava-admin/incident_ops/ (3 files merged) | Combined first_5_minutes + idempotency + error_taxonomy |
| canonical_invariants.md | ava-admin/sync/ | Clean copy with cross-references |

### plan/specs/ava-user/ (9 files)
| File | Source(s) | Notes |
|------|-----------|-------|
| policy_engine_spec.md | ava-user/docs/04 + policies/ | Combined policy engine + requirements |
| receipt_emission_rules.md | ava-user/docs/03 + runtime/ | Combined receipts + runtime integration |
| presence_sessions.md | ava-user/docs/05 + docs/07 | Combined both presence docs |
| approval_binding_spec.md | ava-user/docs/05_approvals | Clean copy with cross-references |
| insight_engine_spec.md | ava-user/insight_engine/ | Enhanced with governance notes |
| ritual_engine_spec.md | ava-user/rituals/ | Enhanced with governance notes |
| architecture.md | ava-user/docs/01 | Includes Mermaid diagram, error codes |
| research_routing_spec.md | ava-user/research/ | Clean copy with cross-references |
| observability.md | ava-user/observability/ + governance/ | Combined observability + governance checklist |

### docs/ava/ (6 files)
| File | Source | Notes |
|------|--------|-------|
| admin/AVA_ADMIN_ENTERPRISE_HANDOFF.md | ava-admin/README/ | Provenance document |
| admin/PHASE_MAPPING.md | ava-admin/roadmap_sync/ + governance/ | Combined with acceptance criteria |
| user/AVA_USER_ENTERPRISE_HANDOFF.md | ava-user/README/ | Provenance document |
| user/ROADMAP_PHASE_ALIGNMENT.md | ava-user/roadmap_sync/ | Enhanced with deliverables per phase |
| CONFLICTS_RESOLVED.md | New | All 7 conflicts documented |
| SYNC_MANIFEST.md | New | This file |

### infrastructure/observability/ (1 file)
| File | Source | Notes |
|------|--------|-------|
| ava_admin_ops_telemetry.md | ava-admin/observability/ | Ops telemetry facade reference |

### tests/fixtures/ava-admin/ (7 files)
| File | Source | Notes |
|------|--------|-------|
| security_negative_cases.md | ava-admin/tests/ | 6 negative test cases |
| injection_strings.txt | ava-admin/tests/fixtures/ | Prompt injection test data |
| secret_samples.txt | ava-admin/tests/fixtures/ | Secret detection test data |
| TEST_PLAN.md | ava-admin/tests/ | Test plan reference |
| examples/change_proposal_global_rollout.json | ava-admin/examples/ | Valid contract example |
| examples/incident_packet_example.json | ava-admin/examples/ | Valid contract example |
| examples/ops_exception_card_example.json | ava-admin/examples/ | Valid contract example |

### tests/fixtures/ava-user/ (6 files)
| File | Source | Notes |
|------|--------|-------|
| AVA_USER_TEST_PLAN.md | ava-user/tests/ | 7 certification test cases |
| examples/ava_request.example.json | ava-user/examples/ | Valid request example |
| examples/ava_result.example.json | ava-user/examples/ | **NORMALIZED: "GREEN" -> "green"** |
| runbooks/01_receipts_down.md | ava-user/runbooks/ | Receipt failure runbook |
| runbooks/02_policy_misconfig.md | ava-user/runbooks/ | Policy misconfiguration runbook |
| runbooks/03_tool_timeouts.md | ava-user/runbooks/ | Tool timeout runbook |

### backend/orchestrator/ava/ (1 file)
| File | Source | Notes |
|------|--------|-------|
| README.md | New | Phase 1 integration notes |

---

## Files NOT Copied (by design)

| Source File | Reason |
|-------------|--------|
| ava-admin/.github/workflows/gates.yml | CI config (we have our own pipeline) |
| ava-admin/ci/gates.yml | CI config (duplicate) |
| ava-admin/prompt_pack/*.md | Prompt pack (absorb into skills, not raw copy) |
| ava-admin/docs/01_role_and_scope.md | Persona doc (absorbed into specs) |
| ava-admin/docs/02_persona_and_tone.md | Persona doc (absorbed into specs) |
| ava-admin/implementation/*.md | Machine-local implementation notes |
| ava-admin/portal_map/admin_portal_map.json | Phase 2 wiring (not needed until then) |
| ava-admin/CHECKSUMS.sha256 | Verification artifact for zip, not repo |
| ava-admin/sync/SYNC_SETUP.md | Zip setup instructions (consumed) |
| ava-user/claude/CLAUDE_CODE_PROMPT.md | Claude instructions (absorbed into CLAUDE.md) |
| ava-user/prompt_pack/system.md | Prompt pack (absorb into skills) |
| ava-user/policies/policy.example.yaml | Example config (absorbed into specs) |
| ava-user/observability/OBSERVABILITY_CHECKLIST.md | Absorbed into specs/ava-user/observability.md |
| ava-user/docs/02_contracts.md | Reference doc (contracts are canonical in plan/contracts/) |
| ava-user/docs/06_tenant_isolation.md | Covered by existing RLS tests + CLAUDE.md Law #6 |
| ava-user/docs/08_observability.md | Absorbed into specs/ava-user/observability.md |
| ava-user/runbooks/RB-01_POLICY_DENIED.md | Duplicate of 02_policy_misconfig format |
| ava-user/runbooks/RB-02_RECEIPT_WRITE_FAILURE.md | Duplicate of 01_receipts_down format |

---

## Normalization Changes Applied

1. **ava_result.schema.json**: `"GREEN"/"YELLOW"/"RED"` -> `"green"/"yellow"/"red"` (Conflict #1)
2. **ava_result.example.json**: `"GREEN"` -> `"green"` in risk.tier field
3. **All specs**: Added cross-references to canonical schemas and implementation targets
4. **Merged files**: Where multiple source files covered the same topic, they were consolidated into single spec files

---

## Re-sync Instructions

If updated versions of either package are released:
1. Extract to `_ava-scan/` as before
2. Diff against this manifest (check for new files, changed schemas)
3. Re-apply normalization rules (risk tier casing)
4. Update this SYNC_MANIFEST.md with new version and date
5. Update CONFLICTS_RESOLVED.md if new conflicts emerge

---

**Total files placed:** 42 (6 contracts + 15 specs + 6 docs + 1 observability + 13 tests/fixtures + 1 backend)
**Total files skipped:** 18 (CI, prompts, duplicates, consumed setup docs)
**Normalizations applied:** 2 (risk tier casing)
**Conflicts resolved:** 7/7
