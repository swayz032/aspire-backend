---
name: wave-17b4-legend
description: Wave 17b.4 legend extraction retune — design choices and verified baselines on GAVNN project 5b0adf45
metadata:
  type: project
---

# Wave 17b.4 — Legend Extraction Retune

## Baseline (verified 2026-05-21 via mcp__supabase__execute_sql)

Project 5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2 (GAVNN, suite 94b89098):
- 39 sheets, 463 blueprint_symbols rows
- 437 still `unknown_motif_*` (94.4% unnamed)
- 241 blueprint_symbol_memory rows for suite — 8 with source='legend', 146 unanchored, 87 tenant_memory

## Why Wave 11h legend extractor under-recovered

**Why:** Wave 11h's `find_legend_regions` only inspected `page.get_text("blocks")` (compact heading bbox) and used a narrow `margin_x=60` lateral search. Many legends sit in a tabular column that extends 200+pt to the right of the heading. The pair extractor's Y-tolerance (12pt) and rightward range (120pt) also dropped rows.

**How to apply:** When a wave reports low recovery against real blueprints, the right move is to (a) widen geometric tolerances calibrated to actual sheet layout (200pt right, 14pt Y-tolerance), (b) extend regex synonyms after observing real headings ("SYMBOL KEY", "ELECTRICAL LEGEND", "HVAC LEGEND"), and (c) cap region expansion at the next major heading instead of arbitrary `expand_down=360`. Also pull headers from `get_text("dict")` spans rather than blocks — blocks collapse a 1-word header into a tiny bbox that hides its position.

## Cross-page legend propagation pattern

**Why:** A sheet's symbol may appear once in a small legend block on sheet 2 but 50+ times on sheets 5-20. Per-page extraction misses this.

**How to apply:** Two-pass orchestration in `vgse_pipeline.py` — pass 1 scans all pages for legend pairs and persists them to `blueprint_symbol_memory` with `source='legend'`. Pass 2 resolves motifs with the project-wide legend table merged into per-page legend. Subsequent projects from the same architect inherit the entire vocabulary instantly.

## Lexicon canonicalisation in legend extraction

**Why:** Legend text "GFCI" should resolve to canonical name `gfci_receptacle`, not the raw uppercase abbreviation. Future tenant_memory lookups must match the binder/spatial-OCR output namespace.

**How to apply:** Pass extracted labels through `symbol_lexicon.lookup(label, discipline=sheet_disc)` before persisting. Fall back to raw label when lexicon misses (better than `unknown_motif_*`).

## Files touched

- `orchestrator/src/aspire_orchestrator/services/blueprint/vgse.py` — region detection, pair extraction, `extract_legend_signatures_with_meta`
- `orchestrator/src/aspire_orchestrator/services/blueprint/vgse_pipeline.py` — two-pass orchestration, legend receipt
- `orchestrator/tests/test_legend_extraction.py` — synthetic-PDF unit tests
- `scripts/backfill_legend_extraction.py` — retroactive runner

## Open work

Pytest + backfill were NOT executed (sandboxed environment had no Python/bash). Operator must:
1. `cd backend/wave-17b4-legend/orchestrator && pytest tests/test_legend_extraction.py -v`
2. `python -m scripts.backfill_legend_extraction 5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2 --suite 94b89098-c4bf-4419-a154-e18d9d53f993 --dry-run`
3. Re-run without `--dry-run` once dry-run looks sane
4. Verify with the SQL in the deliverable report
