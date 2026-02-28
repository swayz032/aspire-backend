# Replay a Trace

## Purpose
Reconstruct and replay a sequence of operations from the Trust Spine receipt chain for debugging, forensics, or incident investigation.

## Inputs
- `trace_id` — the trace identifier linking all related operations in a single execution flow.
- Time window (optional) — to scope the export if trace_id is unavailable.

## Steps

### 1. Export Receipts + Provider Logs
- Query the Trust Spine receipt chain for all receipts matching the `trace_id`.
- Export provider call logs (redacted via DLP/Presidio) for the same trace.
- Receipts are immutable (Law #2) — the export reflects the exact state at execution time.

### 2. Run Replay Harness in SIMULATE Mode
- Feed the exported receipts into the replay harness.
- The harness re-executes the intent through the LangGraph orchestrator in `SIMULATE` mode:
  - Policy checks run normally.
  - Capability tokens are minted but not consumed against real providers.
  - Tool executors return mocked responses based on the original receipt data.
  - New simulation receipts are generated for comparison.

### 3. Compare Outputs
- Diff the original receipts against the simulation receipts.
- Check for: outcome mismatches, missing receipts, unexpected state transitions, policy violations.
- If a mismatch is found: create a failing test case that reproduces the issue.

## Evidence Bundle
The replay produces an evidence bundle containing:
- Original receipts (redacted)
- Simulation receipts
- Diff report
- Provider call log excerpts (redacted)

Store the evidence bundle in `docs/operations/evidence/` for incident postmortems and partner compliance reviews.

## Use Cases
- **Incident investigation**: Replay the failing trace to identify root cause.
- **Partner compliance**: Demonstrate audit trail and replay capability to Gusto/Plaid reviewers.
- **Regression testing**: Convert replay bundles into automated test cases (Gate 1).
