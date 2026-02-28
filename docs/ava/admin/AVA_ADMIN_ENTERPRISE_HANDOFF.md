# Ava Admin -- Enterprise Handoff (v2)

**Synced into Aspire canonical structure: 2026-02-12**
**Original source:** `_ava-scan/ava-admin/`

## Purpose
Ava Admin is Aspire's **control-plane copilot** (internal operator) that:
- **Observes** backend health in near real-time via governed telemetry (read-only by default)
- **Explains** admin portal + infrastructure to a no-code operator (Operator Mode)
- **Proposes** safe changes via versioned ChangeProposals (Engineer Mode available)
- **Runs incidents** and converts them into tests/runbooks (Learning Loop)

## Hard boundaries (fail-closed)
- No direct production mutation without: proposal -> policy -> approval -> outbox -> executor -> receipts
- Tenant isolation via `suite_id` / `office_id` enforced server-side
- Capability tokens are short-lived and scoped (<60s)
- Receipts are append-only; all actions produce receipts (including denials)

## What this zip syncs to
- Aspire Ecosystem pack: v12.7 (generated 2026-02-03)
- Admin Portal routes + Sidebar labels (Operator/Engineer labels)
- Robots + Ops pack (synthetic robots ingest + provider error taxonomy + idempotency)
- Plan artifacts (capability tokens + receipts schemas) and phase docs

## What's new in v2 (production handoff hardening)
- Added deterministic implementation steps (repo-anchored wiring notes).
- Added OpenAPI contract for the Ops Telemetry Facade.
- Added explicit redaction/DLP matrix.
- Added receipt hash-chain + verifier spec.
- Added example payloads and security negative cases.
- Removed machine-local absolute paths.
- Updated ChangeProposal scope contract to support global/suite/office/segment scopes.

## Canonical locations after sync
| Category | Target |
|----------|--------|
| Contracts (4 files) | `plan/contracts/ava-admin/` |
| Runtime/Governance specs (6 files) | `plan/specs/ava-admin/` |
| Observability | `infrastructure/observability/` |
| Test fixtures | `tests/fixtures/ava-admin/` |
| This file | `docs/ava/admin/` |
