---
name: wave-20f-reason-intel-patterns
description: Wave 20f patterns — _build_sheet_intelligence_context helper, stash collision risk on shared branches, pre-commit hook behavior in git.
metadata:
  type: project
---

## Wave 20f: REASON Sheet Intelligence Context

`_build_sheet_intelligence_context(project_id, suite_id, *, sheets, correlation_id) -> tuple[str, dict]`
- Queries `blueprint_sheet_intelligence` with `project_id=eq.{id}&suite_id=eq.{suite_id}` (Law #6 isolation)
- Returns `("", {})` on empty rows — REASON falls back to legacy OCR context (graceful degradation)
- Returns `(context_str, meta)` where meta = `{paragraphs_count, notes_count, legend_count, schedule_count}`
- Context block header MUST contain "AUTHORITATIVE SHEET INTELLIGENCE" (tests assert this literal)
- Legend entries go under "SYMBOL DICTIONARY" header (tests assert this literal)
- Schedule rows report as `row count: N` (tests assert `"row count" in context.lower()`)

**Cover sheet matching:** `sheet_number.upper().startswith(pat.upper())` for `_COVER_PATTERNS` tuple.
**Notes matching:** discipline prefix + rest[0].isdigit() — matches A0.1, FP0.0, S0.0 etc.
**Schedule detection:** tables with headers containing "schedule/door/window/panel/fixture" + regions["schedule"] entries.

**Injection pattern:** Prepend context to call-A and call-B user messages only. Call-C (narrative) is NOT modified — team-lead constraint.

**Receipt:** `action_type = "reason.sheet_intelligence_context"`, `receipt_type = same`, `risk_tier = "GREEN"`, emit even when `context == ""` (has_context=False in metadata).

## Shared Branch Stash Collision Risk

When `git stash pop` on a shared teammate branch, OTHER teammates' unstaged changes may also be restored from the stash (if the stash was created while those files were modified). Symptom: `git diff --stat HEAD` shows more files than expected.

**Fix:** After stash pop, `git restore --staged <other-files>` to unstage teammate files, then restage only your own files.

**Verify before commit:** `git diff HEAD -- <your_file> | wc -l` must be > 0. If 0, your change may already be committed (check `git log --oneline -3`).

## Git Commit Exit Code 1 False Alarm

`git commit` returns exit code 1 when there are remaining unstaged files AFTER a successful commit. The commit itself succeeds. Always verify with `git log --oneline -3` rather than trusting the exit code alone when unstaged files exist.

## PR Timing on Shared Branch

Plan says "open PR after all N commits land." Count commits above base with `git log --oneline origin/<branch> -N`. Only open PR when all planned teammates have pushed their commits. Do not open PR early just because your commit is pushed.
