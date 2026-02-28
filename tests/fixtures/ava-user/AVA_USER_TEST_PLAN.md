# Ava User Certification Test Plan

**Source:** Ava User Enterprise Handoff v1.1

## Test setup (common)
- Use two suites: `suite_A`, `suite_B`
- Use two offices per suite: `office_1`, `office_2`
- Seed receipts ledger with minimal fixtures
- All tests must record `correlation_id` and store receipts

## TC-01 Schema validation (fail closed)
**Given** an invalid AvaOrchestratorRequest (missing `suite_id`)
**When** request hits Orchestrator
**Then** return `SCHEMA_VALIDATION_FAILED` and emit a `decision_intake` receipt with status `denied`.

## TC-02 Tool bypass attempt
**Given** a request that attempts to call a tool not in (role intersection skillpack)
**When** Orchestrator evaluates policy
**Then** return `POLICY_DENIED` and emit `policy_decision` receipt.

## TC-03 Approval missing
**Given** a Yellow-tier plan with no approval
**When** Orchestrator evaluates policy
**Then** return `APPROVAL_REQUIRED` and emit `approval_requested` receipt.

## TC-04 Red-tier without presence
**Given** a Red-tier plan with approval but no presence_token
**When** Orchestrator evaluates policy
**Then** return `PRESENCE_REQUIRED` and emit `presence_missing` receipt.

## TC-05 Capability token expiry
**Given** an execution with an expired capability token
**When** Skill Pack attempts execution
**Then** fail with `CAPABILITY_TOKEN_EXPIRED` and emit `tool_execution` receipt status `denied`.

## TC-06 Cross-tenant access denied
**Given** suite_A request attempts to read suite_B receipts
**When** calling receipts query
**Then** deny with `TENANT_ISOLATION_VIOLATION`.

## TC-07 Research must include citations
**Given** a research request
**When** Research Skill Pack returns output
**Then** output must include citations array and emit `research_run` receipt.

## Exit criteria
All test cases pass. Any failure is stop-ship.
