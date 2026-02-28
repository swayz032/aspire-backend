# Strict Access Checklist — Gusto

## Requirements
- OAuth tokens must be bound to a single Gusto company (maps to Aspire `suite_id` + `office_id`).
- Cross-company access attempts must fail with `denied` outcome and receipt (Law #3 — Fail Closed).
- Provide verification evidence (endpoint test results, RLS isolation test output).

## Implementation (Aspire)
- Store `company_id` on the token record immutably in Supabase, scoped to `suite_id` + `office_id` (Law #6).
- Enforce `company_id` match on every Gusto API request via the Milo skill pack (`backend/orchestrator/services/milo_payroll.py`).
- Capability tokens (Law #5) are scoped to `suite_id` + `office_id` + `tool: gusto.*` — cross-company tokens cannot be minted.
- Automated RLS isolation tests verify cross-company denial (52/52 RLS + evil tests passing).

## Evidence to Attach
- [ ] RLS isolation test report showing zero cross-tenant leakage
- [ ] Evil test results (E1-E11 categories, 63/63 passing)
- [ ] Sample receipt showing `denied` outcome for cross-company access attempt
- [ ] Token audit export showing company-scoped grants
