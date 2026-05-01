---
name: Schema/Code Sync Gaps
description: Fields referenced in Python routes but absent from migrations — a recurring Aspire pattern
type: feedback
---

## greeting_name_override (Pass 18 — sarah.py vs migration 102)

Rule: When a Python route reads a column from a migration via `.get("column_name")`, grep for that column name in ALL migration files for that table before marking the pass complete.
**Why:** `sarah.py` line 305 reads `config.get("greeting_name_override")` but `102_telephony_assignments.sql` has no such column in `front_desk_configs`. The route silently returns empty string every call.
**How to apply:** Before every SHIP gate review, run: `grep -r "column_name" migrations/` to confirm it exists. The Python route is the authoritative "desired schema" doc; the migration must match it.

## General pattern

Team consistently writes the Python service code first (building against desired schema) and the migration second. This creates a window where the code looks correct but the DB schema is incomplete. Always cross-check both directions:
1. Every column accessed in Python → exists in migration
2. Every column in migration → is actually used or intentionally inert
