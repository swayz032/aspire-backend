# Runbook: Receipt Ledger Write Failures

**Source:** Ava User Enterprise Handoff v1.1

## Detection
- Elevated `receipt_write_error` metric
- Orchestrator returning `RECEIPT_WRITE_FAILED`

## Immediate actions
1. Fail closed: disable all execution (draft-only mode).
2. Verify database connectivity and auth.
3. Validate RLS policy changes.

## Recovery
- Restore ledger availability.
- Re-run queued writes from outbox (idempotent).
- Post-incident: verify 0 unreceipted side effects.
