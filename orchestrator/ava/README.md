# Ava Orchestrator Integration

**Status:** Spec sync complete. Implementation starts Phase 1.
**Last updated:** 2026-02-12

## Overview

This directory will contain the LangGraph orchestrator implementation for Ava -- the governed AI execution engine. The contracts and specs have been synced from the Ava Admin v2 and Ava User v1.1 enterprise handoff packages.

## Contracts (source of truth for codegen)

| Contract | Location | Used For |
|----------|----------|----------|
| AvaOrchestratorRequest | `plan/contracts/ava-user/ava_orchestrator_request.schema.json` | POST /v1/intents request body |
| AvaResult | `plan/contracts/ava-user/ava_result.schema.json` | POST /v1/intents response body |
| ChangeProposal | `plan/contracts/ava-admin/change_proposal.schema.json` | Admin change management (Phase 3+) |
| IncidentPacket | `plan/contracts/ava-admin/incident_packet.schema.json` | Incident lifecycle (Phase 3+) |
| OpsExceptionCard | `plan/contracts/ava-admin/ops_exception_card.schema.json` | Proactive anomaly detection |
| OpsTelemetryFacade | `plan/contracts/ava-admin/ops_telemetry_facade.openapi.yaml` | Admin Portal API (Phase 2) |

## Phase 1 Implementation Targets

### Phase 1A -- Substrate Validation (Weeks 3-5)
- [ ] Adopt AvaOrchestratorRequest as POST /v1/intents schema
- [ ] Adopt AvaResult as response schema (lowercase risk tiers)
- [ ] Implement receipt hash chain per `plan/specs/ava-admin/receipt_chain_spec.md`
- [ ] Add chain_id + sequence + prev_hash to receipts table (Migration #50)
- [ ] Implement receipt chain verifier job (5-min interval)
- [ ] Adopt error code taxonomy (SCHEMA_VALIDATION_FAILED, etc.)
- [ ] Create policy_rules table (Migration #51)
- [ ] Implement POST /v1/policy/evaluate

### Phase 1B -- Intelligence Integration (Weeks 4-7)
- [ ] Wire capability token lifecycle per `plan/specs/ava-user/` + canonical schema
- [ ] Implement approval binding with payload-hash integrity
- [ ] Implement presence session binding for red tier
- [ ] Wire OpsExceptionCard emission for receipt chain violations

## Key Specs

| Spec | Location |
|------|----------|
| Receipt hash chain | `plan/specs/ava-admin/receipt_chain_spec.md` |
| Policy engine | `plan/specs/ava-user/policy_engine_spec.md` |
| Receipt emission rules | `plan/specs/ava-user/receipt_emission_rules.md` |
| Approval binding | `plan/specs/ava-user/approval_binding_spec.md` |
| Presence sessions | `plan/specs/ava-user/presence_sessions.md` |
| Architecture (Mermaid) | `plan/specs/ava-user/architecture.md` |
| DLP redaction | `plan/specs/ava-admin/dlp_redaction_matrix.md` |

## Test Fixtures

- Admin security negatives: `tests/fixtures/ava-admin/security_negative_cases.md`
- User certification tests: `tests/fixtures/ava-user/AVA_USER_TEST_PLAN.md`
- Example payloads: `tests/fixtures/ava-{admin,user}/examples/`
- Runbooks: `tests/fixtures/ava-user/runbooks/`

## Governance

All implementation must comply with the 7 Immutable Laws (CLAUDE.md).
Conflict resolutions documented in `docs/ava/CONFLICTS_RESOLVED.md`.
Full sync manifest: `docs/ava/SYNC_MANIFEST.md`.
