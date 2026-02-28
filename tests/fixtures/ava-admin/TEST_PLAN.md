# Test Plan (Ava Admin)

**Source:** Ava Admin Enterprise Handoff v2

## Security
- Prompt injection via logs/provider payloads does not alter tool permissions
- Secrets never appear in LLM-visible telemetry
- Cross-tenant access is denied (suite_id boundary)

## Governance
- Approval missing -> action denied + denial receipt
- Expired capability token -> action denied
- Unknown risk -> deny or require approval

## Reliability
- Outbox lag detector fires on threshold breach
- Provider error spikes produce OpsExceptionCard
- Rollout canary degradation triggers rollback path

## Determinism
- Same incident evidence pack produces same recommended mitigation ordering (within tolerance)

## Fixtures
- `tests/fixtures/ava-admin/` contains injection and secret samples for redaction + inertness tests.
- Example payloads: `tests/fixtures/ava-admin/examples/`
