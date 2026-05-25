# Blueprint Pipeline — End-to-End Test Harness

**Purpose:** Walk through the full Drew blueprint pipeline like a contractor would. **Every stage emits visual artifacts that a human MUST inspect before declaring the stage passed.** This is the zero-false-positive guarantee — if you can't SEE the output is correct, it isn't.

**Anti-pattern this prevents:** "Tests pass + metrics look good but the system is producing garbage." That's the historic failure mode of CV pipelines. Visual evidence makes garbage visible.

**Reference project:** Gulfstream `7a93d144-5d53-47d3-8b97-067ebe249d88` (39 sheets, ~62K motif instances, 288 legend symbols).

---

## Pre-Flight (run before any test)

```bash
cd C:/Users/tonio/Projects/myapp/backend
export RAILWAY_TOKEN="0736f42a-dd89-46d0-bfa7-7d7ebe494b16"
git checkout main && git pull origin main
git log --oneline -5    # confirm latest waves merged
```

**Artifact dir:** `C:\Users\tonio\Pictures\wave-20-debug\` — every stage writes inspectable PNGs/JSON here. Wipe between full runs.

**Reset Supabase for the project before fresh run:**
```sql
DELETE FROM blueprint_resolved_symbols WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88';
DELETE FROM blueprint_project_legends WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88';
DELETE FROM blueprint_sheet_intelligence WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88';
-- (do NOT delete blueprint_symbols — that's Wave 11g-v2 source data, expensive to regenerate)
```

---

## Stage 1 — INGEST (PDF upload → sheets)

**Run:**
- Either upload a fresh PDF via `/v1/blueprints/upload-init` + `/upload-complete` (real user path), OR re-trigger an existing project via Drew dispatch.

**Verify:**
| Check | Pass if | Visual artifact |
|---|---|---|
| Sheet count | `SELECT COUNT(*) FROM blueprint_sheets WHERE project_id = ...` matches PDF page count | none — DB count |
| Sheet PNGs stored | Each `blueprint_sheets.image_storage_path` is non-null + downloadable | open 3 random sheet PNGs from Supabase Storage |
| Stage progress | `stage_progress.ingest = 'done'` | none |

**False-positive trap:** PDF rendering can silently produce blank PNGs if PyMuPDF version is wrong. **Look at 3 random sheet PNGs in the Supabase Storage UI.** If any are blank or mostly-white when the source PDF has content → FAIL.

---

## Stage 2 — UNIVERSAL SHEET INTELLIGENCE (Wave 20)

**Run:**
```bash
railway run python orchestrator/scripts/backfill_wave_20_sheet_intelligence.py \
  --project-id 7a93d144-5d53-47d3-8b97-067ebe249d88
```

**Verify:**
```sql
SELECT
  COUNT(*) AS sheets,
  SUM(jsonb_array_length(tables)) AS tables_extracted,
  SUM(jsonb_array_length(paragraphs)) AS paragraphs_extracted,
  SUM(jsonb_array_length(regions->'legend')) AS legend_regions,
  SUM(jsonb_array_length(regions->'schedule')) AS schedule_regions,
  SUM(jsonb_array_length(regions->'notes')) AS notes_regions
FROM blueprint_sheet_intelligence
WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88';
```

| Check | Pass band |
|---|---|
| sheets | matches PDF page count |
| tables_extracted | ≥ 1 per sheet on average |
| paragraphs_extracted | ≥ 100 (text-heavy commercial blueprint) |
| legend_regions | 10–60 (commercial project) |
| schedule_regions | 5–50 (commercial project) |
| notes_regions | 10–80 |

**Visual artifact required:** For Wave 20.13+ — script `debug_sheet_intel_overlay.py` should produce one PNG per sheet showing Azure-detected paragraph + table polygons drawn on top of the sheet. **Open 3 random overlays. Every visible block of text on the source sheet must have a corresponding overlay box.** If overlays are wrong/missing → FAIL.

**False-positive trap:** Region classifier may tag revision-history tables as "legend". Inspect:
```sql
SELECT label FROM blueprint_project_legends
WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88'
ORDER BY random() LIMIT 20;
```
**Every label MUST be a real construction symbol name** (FLOOR DRAIN, JUNCTION BOX, URINAL, etc.). NOT revision notes ("UPDATED PROJECT ADDRESS", "REV 1"). NOT pure numbers ("0.2", "1.0"). If ≥10% are noise → tighten region classifier, do NOT proceed.

---

## Stage 3 — LEGEND EXTRACTION (Wave 20.6.1)

**Run:**
```bash
railway run python orchestrator/scripts/validate_wave_20_6.py
```

**Verify counts:**
```sql
SELECT
  COUNT(*) AS total_legends,
  COUNT(DISTINCT discipline) AS disciplines,
  COUNT(*) FILTER (WHERE vector_signature IS NOT NULL) AS embedded,
  COUNT(*) FILTER (WHERE label ~ '^[A-Z][A-Z0-9 \-/]+$') AS uppercase_labels_count
FROM blueprint_project_legends
WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88';
```

| Check | Pass band |
|---|---|
| total_legends | 50–500 (typical commercial) |
| disciplines | ≥ 2 |
| embedded | = total_legends (all backfilled) |
| uppercase_labels_count | ≥ 50% of total (construction symbols use ALL CAPS conventions) |

**Visual artifact required:** Script `debug_legend_crops.py` writes one PNG per top-30 legend entry to `wave-20-debug/legends/`. **Open the folder. Each PNG must clearly show ONE symbol glyph + its label text.** If crops are whitespace or contain unrelated content → legend bbox extraction is broken → FAIL.

**False-positive trap:** Legend `representation_bbox` may point at whole-table polygon (8-coord) instead of per-cell. Crops will be huge tables instead of single symbols. The Wave 20.6.1a-fix should have prevented this — verify cell-level bboxes.

---

## Stage 4 — WAVE 11g-v2 SEE / SYMBOL DETECTION

This stage is already-shipped Wave 11g-v2. It produces `blueprint_symbols` rows (vector primitive motifs, NOT composed symbols — see Wave 20.13 known gap).

**Verify:**
```sql
SELECT
  COUNT(*) AS motif_rows,
  COUNT(DISTINCT bbox->>'signature_hash') AS unique_signatures,
  SUM((bbox->>'instance_count')::int) AS total_instances,
  COUNT(*) FILTER (WHERE bbox->>'original_class' NOT LIKE 'unknown_motif%') AS named_motifs
FROM blueprint_symbols bs
WHERE bs.sheet_id IN (
  SELECT id FROM blueprint_sheets WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88'
);
```

| Check | Pass band (Gulfstream baseline) |
|---|---|
| motif_rows | 5K–20K |
| unique_signatures | 200–500 |
| total_instances | 30K–80K |
| named_motifs | ≥ 50 |

**Visual artifact required (Wave 20.13+):** `debug_motif_overlay.py` writes per-sheet PNGs showing each detected motif bbox as a small colored rectangle on the source sheet. **Open the FP-1 (fire protection) sheet overlay. Visible sprinklers in the drawing must have overlay boxes. Visible walls/dimensions should NOT have many overlay boxes.** If wall lines are heavily annotated → primitive composition is needed (Wave 20.13).

**KNOWN GAP:** Wave 11g-v2 outputs primitive fragments, not composed symbols. A sprinkler = ~5–8 fragments. This is the next breakthrough wave (Wave 20.13 — Primitive Composition Engine per GPT v9 guidance).

---

## Stage 5 — TERMINAL RESOLVER LAYER (Wave 20.8)

**Run:**
```bash
railway run python orchestrator/scripts/validate_tr_layer.py
```

**Verify 100% terminal coverage (HARD INVARIANT):**
```sql
SELECT terminal_state, COUNT(*), SUM((bbox->>'instance_count')::int) AS inst
FROM blueprint_resolved_symbols
WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88'
GROUP BY 1 ORDER BY COUNT(*) DESC;
```

| Check | Pass condition |
|---|---|
| Sum of rows | = total motif rows from Stage 4 (NO dangling) |
| All terminal_state values | ∈ {BOUND_MATERIAL, DECORATION, TEXT_OCR, PLAN_ELEMENT, CRUFT, UNKNOWN_REVIEW} |
| No NULL terminal_state | DB CHECK constraint prevents this — verify by `WHERE terminal_state IS NULL` returns 0 |

**Visual artifact required:** `debug_terminal_state_overlay.py` writes per-sheet color-coded PNG:
- 🟢 Green box = BOUND_MATERIAL
- 🔵 Blue = TEXT_OCR
- 🟡 Yellow = PLAN_ELEMENT
- ⚪ Gray = CRUFT
- 🟣 Purple = DECORATION
- 🔴 Red = UNKNOWN_REVIEW

**Open the overlay for FP-1 (fire protection). Inspect:**
- Sprinkler symbols → should be 🟢 (or 🔴 if Wave 20.13 not shipped)
- Door labels (e.g. "101", "A-1") → should be 🟡 PLAN_ELEMENT
- General notes paragraph → bboxes should be 🔵 TEXT_OCR
- Title block, sheet border → 🟡 or ⚪
- Hatching fills → ⚪ CRUFT

**False-positive trap:** If a sprinkler symbol on the drawing is tagged 🔴 UNKNOWN_REVIEW even though the legend has "EXISTING UPRIGHT SPRINKLER" → Wave 20.13 composition layer is missing (or signature_hash drift).

---

## Stage 6 — VLM CONSENSUS TIEBREAKER (Wave 20.12)

**Run:**
```bash
railway run python orchestrator/scripts/validate_consensus.py
```

**Verify VLM precision (zero-false-positives guarantee):**
| Check | Pass condition |
|---|---|
| families_bound > 0 OR families_examined = 0 | If examined > 0 but bound = 0 → VLM is over-rejecting (crop bug OR motif fragmentation) |
| Per-family pos_ratio + variance logged | Each binding decision shows the consensus math |
| Total cost ≤ $0.50 | Hard cap respected |

**Visual artifact required:** `debug_vlm_audit_cards.py` writes one HTML page per VLM call showing:
- Left: motif crop (what VLM saw on the sheet)
- Right: legend crop (what VLM compared against)
- Verdict: YES / NO
- Reason: VLM's one-sentence explanation
- Decision: BOUND / REJECTED

**Open 10 random VLM audit cards. Inspect:**
- If VLM said YES → the two crops MUST visually look like the same symbol. If not → FALSE POSITIVE — fail the binding, mark for review.
- If VLM said NO → the crops should obviously differ. If they look like the same symbol but VLM said NO → cropping is wrong, not VLM.

**This audit card review is the contractor-trust layer. Without it, the system is opaque.**

---

## Stage 7 — REASON + PROCURE (downstream of TR-Layer)

**Run:** triggered automatically by Drew pipeline.

**Verify:**
```sql
SELECT
  COUNT(*) AS material_rows,
  COUNT(*) FILTER (WHERE jsonb_array_length(suppliers_ranked) > 0) AS with_suppliers,
  COUNT(*) FILTER (WHERE confidence >= 0.7) AS high_conf
FROM blueprint_materials
WHERE project_id = '7a93d144-5d53-47d3-8b97-067ebe249d88';
```

| Check | Pass band |
|---|---|
| material_rows | 50–500 (commercial scope) |
| with_suppliers | ≥ 60% of material_rows |
| high_conf | ≥ 30% |

**Visual artifact required:** Open the Aspire desktop app → Estimate Studio → Takeoff → Material Story chip. Walk through 10 random material cards. **Each card must show:**
- Real material name (not "unknown_motif_*")
- Quantity with units
- ≥ 1 supplier with price
- Citation chain (which sheet, which region)

**If any card is gibberish → FAIL. That's the false positive surface where a contractor would lose trust.**

---

## Stage 8 — CONTRACTOR EXPERIENCE WALKTHROUGH (the user POV test)

This is the final acceptance gate. **Open the Aspire desktop app as Tonio Scott (`tonioscott58@yahoo.com`)**. Walk through as if you were a contractor receiving a new blueprint:

1. **Service Hub → Estimate Studio → Plans & Photos:** Does the uploaded blueprint show its sheets? Are thumbnails recognizable? ✅/❌
2. **Toggle to Scope tab:** Story chip — does Drew's narrative read like a real construction overview, NOT generic boilerplate? ✅/❌
3. **The Buy List chip:** Are line items real materials? Random-spot-check 5 — would you actually order these from a supplier? ✅/❌
4. **Need to Confirm chip:** Are the missing inputs ACTUAL gaps (job-site address, specific finishes)? NOT system errors masquerading as missing inputs? ✅/❌
5. **Takeoff tab → Material Story:** Each card has a clear symbol name, count, citation. Random-spot 5 cards. ✅/❌
6. **Takeoff → Symbol Legend chip:** Does it match the legend you'd see on the actual blueprint? ✅/❌

**If any step is ❌, the production system has false positives even if the database metrics look good.** Hold deployment.

---

## Closing — what counts as "DONE"

A test pass = ALL of:
- ✅ 100% terminal state coverage (DB query)
- ✅ Visual artifacts at every stage match the source PDF (human inspection)
- ✅ Zero false positives in VLM audit cards (random sample of 10)
- ✅ Contractor POV walkthrough passes all 6 checkpoints
- ✅ No regression vs prior baseline (KPI delta logged)

If any single visual artifact looks wrong → STOP. Diagnose. Do NOT trust the metrics.

**The visual evidence harness is the no-false-positives guarantee. Without it, the system is opaque and metrics lie.**
