# Ava User -- Enterprise Handoff (v1.1)

**Synced into Aspire canonical structure: 2026-02-12**
**Original source:** `_ava-scan/ava-user/ava-user-enterprise-handoff_v1.1/`

## Objective
Implement a production-ready **user-facing Ava** (SMB Executive Assistant / Chief of Staff) under Aspire's governed execution model.

## Scope
### In-scope
- Orchestrator request/response contracts
- Policy + approval gating
- Capability tokens (server-side only)
- Receipt emission for every state change and every decision
- Tenant isolation (suite_id + office_id boundaries)
- Research routing (read-only) and ResearchReceipt emission
- Exception Cards + Ritual outputs as *draft artifacts* unless explicitly approved for execution

### Out-of-scope
- Admin Portal features
- Billing, pricing enforcement, marketing UX
- Third-party tool integrations beyond stubbing (Stripe/Gmail/etc.)

## Non-negotiable invariants
1. **Fail-closed:** any ambiguity, schema mismatch, or missing gate => deny.
2. **Deny-by-default tools:** no tool executes unless explicitly allowed by policy and a valid capability token is present.
3. **Approvals:** any action that can change external state or has meaningful risk requires explicit approval.
4. **Presence:** red-tier actions require a verifiable user presence session.
5. **Receipts:** no execution without a receipt; no response claiming execution without a receipt.
6. **Tenant isolation:** suite_id is the company boundary; office_id is the seat boundary; zero cross-tenant leakage.

## Definitions
- **suite_id:** company / tenant identifier
- **office_id:** seat identifier (member/team within suite)
- **correlation_id:** trace id shared across orchestrator -> policy -> tool -> receipts
- **request_id:** unique id for a single orchestrator request (idempotency key)

## Canonical locations after sync
| Category | Target |
|----------|--------|
| Contracts (2 files) | `plan/contracts/ava-user/` |
| Runtime/Governance specs (9 files) | `plan/specs/ava-user/` |
| Test fixtures + runbooks | `tests/fixtures/ava-user/` |
| This file | `docs/ava/user/` |
