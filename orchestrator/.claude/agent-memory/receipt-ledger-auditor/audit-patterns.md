---
name: audit-patterns
description: Receipt schema patterns, known gaps, and recurring issues across the Aspire codebase
type: project
---

# Receipt Patterns

## Backend Receipt Schema (Python)
Standard fields per `_build_receipt()` in intents.py and `_build_access_receipt()` in admin.py:
- id, correlation_id, suite_id, office_id, actor_type, actor_id
- action_type, risk_tier, tool_used, outcome, reason_code
- created_at, receipt_type, receipt_hash (computed SHA256)
- redacted_inputs, redacted_outputs

## Desktop Receipt Schema (TypeScript)
`emitReceipt()` in routes.ts writes to `receipts` table with:
- receipt_id, receipt_type, status (SUCCEEDED/FAILED/DENIED)
- action (JSONB), result (JSONB), suite_id, tenant_id
- correlation_id, actor_type, actor_id, created_at

`createTrustSpineReceipt()` is used for booking.checkout and booking.confirm flows.

## Known Systemic Debt (excluded from cycle reports)
- receipt_hash = "" across all provider skillpacks (separate work item)
- 42% receipt coverage (55/130 ops) — systemic gap tracked separately
- 9 rule-based skillpacks never call store_receipts() — tracked separately

## Recurring Gap Patterns
1. **Webhook handlers build receipt dicts but never persist them** (PandaDoc, Twilio)
2. **Telemetry/observability write paths lack receipts** (client_events ingest)
3. **Auth endpoints (signup, invite-code)** never emit receipts
4. **Admin Ava Desk endpoints** (triage, provider-analysis) emit denial receipts but skip success receipts
5. **Outer catch blocks** log but silently swallow failure receipt DB writes
6. **Fire-and-forget receipt emission** with `.catch(() => {})` drops failures silently

## Trace Chain Patterns
- correlation_id propagated correctly from Gateway headers through all backend routes
- Desktop uses `x-correlation-id` header or generates `corr-{type}-{uuid}`
- a2a service provides receipt_data via result object — endpoints store only if result.receipt_data is not None
- SSE stream receipts: initiate + complete + error + cancelled all covered
