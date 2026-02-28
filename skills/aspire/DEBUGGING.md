# Aspire Debugging Skill (DEBUGGING.md)

## Goal
Reduce repeated debugging loops by enforcing a consistent diagnose → patch → verify workflow.

## Standard workflow
1. **Reproduce**
   - Capture steps, environment, and expected vs actual.
2. **Collect Evidence**
   - Receipt, logs (by correlationId), UI state snapshot, server trace.
3. **Localize**
   - Identify boundary: UI, API, worker, DB, integrations.
4. **Patch**
   - Smallest change that fixes root cause.
5. **Verify**
   - Add/extend tests; confirm no regression.
6. **Document**
   - Update skills if the issue is “repeatable” (pattern).

## Repeat-correction rule
If a correction is made **twice**, add it to the relevant skill file:
- STYLE for conventions
- RECEIPTS for schema/receipt patterns
- SAFETY for guardrails
- DEBUGGING for workflow patterns

## Update Policy
- All skill updates require diffs and Git history.
- Avoid bloat: consolidate duplicates quarterly.

## Changelog
- (append entries here)
