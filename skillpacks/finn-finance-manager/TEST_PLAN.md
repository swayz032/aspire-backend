# Finn Finance Manager — Test Plan

## 1) Unit tests
- Inputs hashing is stable for proposals (sha256 deterministic)
- Risk tier mapping: `required_approval_mode` aligns to tier
- Output validates against `schemas/06_output_schema.json`

## 2) Integration tests (API)

### Tenant isolation
- Suite A cannot read Suite B snapshot, exceptions, or proposals
- Missing tenant headers → 401/403 fail-closed

### Stale data behavior
- If any lane freshness exceeds threshold:
  - Exception is emitted
  - Finn avoids numeric claims for that lane

### Authority binding
- Approve/deny transitions emit receipts
- Denied items cannot proceed to execution

### A2A delegation
- Delegation to allowlisted agent → accepted
- Delegation to non-allowlisted agent → policy denied + receipt
- Delegation depth > 2 → denied

## 3) Security tests
- Prompt injection in invoice memo cannot cause proposal creation outside allowlist
- Prompt injection cannot bypass approvals
- Tenant identity spoofing (X-Suite-ID header) → denied with receipt
- Schema validation: malformed output missing `suite_id` → fails + deny receipt

## 4) UX acceptance tests
- Right rail renders on Finance Hub pages
- Finn Desk loads snapshot/exceptions and displays timestamps
- Authority Queue shows pending finance proposals
- Approve/deny buttons work and produce receipts

## 5) Regression tests
- Finn Money Desk unchanged behavior
- Existing Finance Hub pages still render
- Existing snapshot engine computations unchanged

## 6) Receipt coverage
For each required event type:
- Receipt exists
- Contains tenant fields, correlation_id
- Contains inputs_hash (and outputs_hash when applicable)
- Validates against `schemas/receipt_event.schema.json`
