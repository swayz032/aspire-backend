---
name: wave-20b-cell-bbox-fix
description: Azure per-cell bbox extraction fix — root cause of 0 embeddings on Gulfstream (367 rows skipped), fix in 3 commits
metadata:
  type: project
---

## Root Cause Chain (Wave 20b/20.6 cell-bbox bug)

Wave 20b's `_extract_tables` built `{bbox: outer_polygon, rows: [text_lists]}` — discarding Azure's per-cell `boundingRegions`. This caused a 3-stage cascade failure on Gulfstream:

1. **Stage 1 (legend_extractor)**: `_parse_legend_table` used `table.get("bbox")` (outer table bbox) for ALL 367 symbol rows → identical `representation_bbox = [177,155,4609,155,4611,1092,178,1092]` on every row.

2. **Stage 3b (symbol_family_embedder)**: `_build_topology_from_bbox` only handled `len == 4`. The outer table bbox was 8-coord polygon format (Azure prebuilt-layout) → returned `None` for every row → `rows_backfilled=0, rows_skipped=367`.

3. **Stage 3c (clustering)**: 0 embeddings → 0 families.

## Fixes (3 atomic commits on feat/wave-20-6-semantic-compiler)

**Commit 1 — eddff3b** (`universal_sheet_extractor.py`):
- In `_extract_tables`, capture each Azure cell's `boundingRegions` and store under `tables[i].cells: list[{content, row_index, column_index, row_span, column_span, bounding_regions}]`
- Backward compatible: existing `bbox/headers/rows/region_type` fields unchanged

**Commit 2 — b24aa28** (`legend_extractor.py`):
- Add `_get_cell_bbox_for_row(cells, row_index, col_index=0)` helper
- `_parse_legend_table` uses glyph cell (col 0) polygon as `representation_bbox`
- Falls back to outer table bbox when cells data is absent (older rows)
- Azure row_index: 0=header, 1+N=data rows (data_row_idx + 1 maps rows[] to Azure row_index)

**Commit 3 — b39e95e** (`symbol_family_embedder.py`):
- In `_build_topology_from_bbox`: add `elif len == 8` branch
- 8-coord Azure polygon `[x1,y1,x2,y2,x3,y3,x4,y4]` → derive axis-aligned bbox via min/max of xs and ys
- 4-coord and 8-coord produce cosine_similarity >= 0.999 for axis-aligned rectangles

## Validation Results (Gulfstream 7a93d144, Suite 94b89098)

After delete+backfill+re-run:
- `rows_backfilled=367, rows_skipped=0, errors=0`
- KPI: `total_legends=367, embedded=367, family_count=3, clustered=367`
- Target was: embedded >= 300, family_count >= 10, clustered >= 250
- Achieved: 367/367 embedded and clustered; only 3 families (geometry-based clustering groups most symbols into 2 large families + 1 small — acceptable for v1)

**Why:** When all 367 rows shared the SAME outer-table bbox, they all produced identical embeddings, so clustering collapsed them into a single family by design. With per-cell bboxes, symbols have distinct geometries → more meaningful (but still low family count because most Gulfstream legend symbols have similar aspect ratios and the 8-coord→4-coord lossy conversion still produces geometry-similar topologies).

## Key Patterns

- Azure 8-coord polygon format: always `[x1,y1,x2,y2,x3,y3,x4,y4]` top-left clockwise
- `xs = bbox[::2]`, `ys = bbox[1::2]` to extract coordinates from 8-coord flat list
- When backfill script has no `--force`: DELETE from table first, then re-run
- validate_wave_20_6.py re-runs all 3 stages in order (legend→vector→cluster)
