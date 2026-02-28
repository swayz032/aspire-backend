# Aspire Receipts Skill (RECEIPTS.md)

## Purpose
Standardize how Aspire records execution and debugging evidence.

## Receipt invariants (do not break)
- Every run emits a receipt with a stable schema.
- Every receipt includes a **correlationId** that ties together logs, events, and UI traces.
- Receipts are append-only; no destructive edits in-place.

## Minimal receipt fields
- `receiptId`
- `correlationId`
- `timestampStart`, `timestampEnd`
- `actor` (human/system/agent)
- `inputs` (redacted)
- `actions`
- `outputs` (redacted)
- `status` (success|failed|partial)
- `errors` (if any)
- `artifacts` (links/paths/hashes)

## Incident linkage
- If `status != success`, create or attach to an `incidentId`.

## Update Policy
- Propose changes via diff
- Keep schema backward-compatible
- Document migrations if needed

## Changelog
- (append entries here)
