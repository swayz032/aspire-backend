---
name: wave-20-6-legend-extractor-patterns
description: Wave 20.6 Stage 1 legend extractor — real legend region schema, discipline detection, table parsing, upsert patterns
metadata:
  type: project
---

## Legend Region Schema (from real Gulfstream data)

`blueprint_sheet_intelligence.regions['legend']` entries have shape:
```json
{"bbox": [x0,y0,x1,y1,x2,y2,x3,y3], "table_index": 0}
```
`table_index` is an integer index into `bsi.tables[]`. NOT a separate table — it points into the same row's `tables` JSONB array.

`tables[i]` has: `{bbox, headers, rows: [[cell,...]], region_type: "legend"}`

## Gulfstream Project Stats
- project_id: `7a93d144-5d53-47d3-8b97-067ebe249d88`
- suite_id: `94b89098-c4bf-4419-a154-e18d9d53f993`
- 39 sheets, 51 legend regions across 17 sheets
- Most sheets have `discipline=null` in blueprint_sheets — fallback to sheet_number prefix needed

## Discipline Detection Priority
1. `blueprint_sheets.discipline` DB enum (A/S/M/E/P/FP/C) → canonical name
2. `sheet_number` prefix match (FP* > E* > M* > P* > A* > S* > C*) — FP MUST be before E/P
3. "unknown" fallback

## Table Row Parsing Convention
- col 0 → symbol_id (skip rows where empty/whitespace — Azure header repetitions)
- col 1 → label
- col 2+ → specification (join non-empty cells with space)
- representation_bbox = the table's bbox (not per-row; one bbox per legend table)

## Upsert Pattern
Use `supabase_insert_batch_ignore_conflicts(table, rows, conflict_target="project_id,discipline,symbol_id")`
Returns `(inserted_rows, duplicate_count)` — never raises on conflict.
CRITICAL: `return=minimal` NOT needed here — `ignore_conflicts` handles the 406 risk.

## blueprint_project_legends Table
- Migration: `20260524000003_blueprint_project_legends.sql`
- UNIQUE: `(project_id, discipline, symbol_id)`
- `vector_signature` NULL until Stage 3 (Symbol Family Embeddings)
- `source_sheet_id` FK → blueprint_sheets (nullable)
- `source_region_index` int — which legend region within the sheet

## Test Patterns That Work
- `_fake_select_factory` pattern: async factory returning a routing function by table name
- Suite isolation: check `_SUITE_OTHER in filters` → return [] (simulates RLS)
- Empty legend early exit: receipt still fires even when 0 legends found

**Why:** Stage 1 downstream consumers (Stages 2-6) read from blueprint_project_legends as ground truth.
**How to apply:** When building Stage 2+ adapters, query blueprint_project_legends first (not blueprint_sheet_intelligence legends directly).
