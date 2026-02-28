# Launch Gate Checklist (High-Risk Workflow System)

This is the release “stop/go” checklist. If any item is red, you do not ship high-risk automation.

## A) Safety enforcement
- [ ] High-risk approvals require Ava Video presence (server enforced)
- [ ] Execution mode works: ENABLED / APPROVAL_ONLY / DISABLED
- [ ] No shadow execution paths (static gate + runtime invariant)

## B) Execution reliability
- [ ] Transactional outbox pattern is used (jobs created with DB state)
- [ ] Idempotency keys exist for all provider side effects
- [ ] Retries do not duplicate side effects

## C) Auditability
- [ ] Receipts emitted for: session start, approval decision, execution outcome
- [ ] Receipts link to: authority item, approval, presence session, outbox job, provider call refs
- [ ] Replay can reconstruct at least 1 golden workflow from trace_id

## D) Tenant isolation
- [ ] RLS enabled for all tenant-bound tables
- [ ] RLS “evil tests” pass in CI

## E) Ops readiness
- [ ] Kill switch runbook exists and is tested
- [ ] Incident response runbook exists
- [ ] Support bundle export exists (receipt + trace bundle)

## F) CI gates
- [ ] Migrations apply clean on fresh DB
- [ ] Unit + integration tests pass
- [ ] Eval regression gate (if applicable) passes
