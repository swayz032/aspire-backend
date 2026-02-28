# Canonical Invariants (from Ecosystem MANIFEST.json)

**Source:** Ava Admin Enterprise Handoff v2

## Invariants
- proposal -> approval -> outbox -> execution -> receipts
- no shadow execution paths
- fail closed on unknown risk
- idempotency required for provider writes
- tenant isolation via suite_id
- primary->critic->fix->gate before approval

## The Only Allowed Side-Effect Pipeline

```
[Desk / Skill Pack]
      |
      v
  Proposal (intent)
      |
      v
Policy evaluation (risk + required approvals)
      |
      +---------------------+
      |                     |
      v                     v
Approval required?        No approval required
      |                     |
      v                     v
Approval recorded        (continue)
      |
      v
Outbox job created  --->  Executor consumes job
      |
      v
Provider call (idempotent)
      |
      v
Provider Call Log written
      |
      v
Receipts emitted:
- proposal.*
- approval.* (if required)
- execution.*
```

## Guardrails
- **Fail closed**: unknown policy/risk => require approval or deny.
- **Idempotency**: every provider write must be idempotent.
- **Tenant isolation**: every step scopes to `suite_id`.

## Cross-reference
- CLAUDE.md Prime Directive (Law #0)
- Aspire 7 Immutable Laws
