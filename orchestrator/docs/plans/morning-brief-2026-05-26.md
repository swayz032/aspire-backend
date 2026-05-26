# Morning Brief — 2026-05-26 (overnight upload audit)

## TL;DR
- **Track B platform hardening shipped and FIRED in production on first real run** ✅
- **Azure DocIntel rotated, working** (9.7K OCR chars on cover sheet) ✅
- **Drew honestly admits he can't read civil engineering drawings** ⚠️ ← real R&D signal

## Project under audit

| Field | Value |
|---|---|
| project_id | `73d70ed2-da8a-44d1-a811-3e93ed946ca9` |
| PDF | `21030 ENG_Rev1 240801_Signed.pdf` (Mangonia Park, civil engineering, McLeod McCarthy & Associates) |
| Address | `null` (was not typed; user uploaded without filling address field) |
| Created | 2026-05-26 19:20:01 UTC |
| Suite | `94b89098-c4bf-4419-a154-e18d9d53f993` |

## Stage progress

```
ingest:   done    ✅
classify: done    ✅
see:      in_progress   ← stuck flag; substages all done
reason:   done    ✅ (ran via progressive dispatch)
procure:  done    ✅ (ran via progressive dispatch)
```

## Output volumes

| Table | Count | Expected for a 13-sheet civil plan |
|---|---|---|
| blueprint_sheets | 13 | 13 ✅ |
| blueprint_materials | 0 | ~50-200 (FAIL) |
| blueprint_assemblies | 0 | ~10-30 (FAIL) |
| blueprint_primitives | 0 | ~5K-50K (FAIL) |
| blueprint_resolved_symbols | 0 | ~100-1000 (FAIL) |
| blueprint_project_legends | 0 | ~1-3 (FAIL) |
| blueprint_semantic_edges | 0 | ~500-5000 (FAIL) |
| blueprint_missing_inputs | 16 | honest report |

## What FIRED correctly — platform hardening proven in prod

Two `SUCCEEDED_WITH_ZERO_OUTPUT` receipts at 19:21:42 — the **first-ever production firings** of the status we added to the CHECK constraint 4 hours ago in Track B Pass 1.C:

```
action_type                       status                          time
drew.see.spatial_binding          SUCCEEDED_WITH_ZERO_OUTPUT      19:21:42
drew.see.legend_extracted         SUCCEEDED_WITH_ZERO_OUTPUT      19:21:42
blueprint.see.vgse.completed      SUCCEEDED                       19:21:54
```

This is **exactly the failure mode FU-N-4.9 was designed to expose**. Before today, those would have been written as `SUCCEEDED` (lying with a green status while writing zero rows). Now they're loud and visible. Track B works.

Azure DocIntel post-rotation: 9,733 chars OCR on cover sheet C2.3, 1.2K-3K on interior sheets. Key 1 on the new tenant works fine; no rate-limit issues seen on F0 (~13 calls).

## What Drew honestly told us he can't do

The 16 `blueprint_missing_inputs` rows are a high-quality R&D signal — not noise:

```
"Sheet N: insufficient evidence to confirm discipline (assigned C) —
 no symbols detected yet, sparse OCR, no numeric neighbors. Contractor: please verify."

"Sheets 1, 5, 6, 7, 8, 9, 10, 12, and 13 show no extracted symbol detections..."

"Re-run symbol detection or inspect sheet graphics manually to confirm
 whether features were missed."
```

Drew's self-verification (Wave 17b.6) is doing what it was built to do: surface the gap honestly rather than hide it. Per Aspire Law #3 (Fail Closed), this is the correct behavior.

## What this reveals about Drew

**"Drew can read any blueprint" claim is currently FALSE on civil engineering plans.**

Specifically:
1. Wave 11g-v2 (vector graph hashing SEE) returned zero motifs on civil drawings — civil plans are continuous line-work (contour lines, alignments, grading sections), not the discrete-symbol motifs Wave 11g-v2 was trained on
2. No legend extraction succeeded — civil sheets typically don't have a tabular symbol legend; they use callout notes referencing detail numbers
3. The cascade resolvers couldn't determine discipline beyond what `sheet_code` regex gave (`C*` → Civil)

This matches the v9 roadmap's call: **Wave 20.6 Construction Semantic Compiler** is needed for non-MEP disciplines. Civil = different glyph system + dense line work + callout-driven (no legend block).

## Operational anomalies worth flagging

- **`stage_progress.see` flag is stuck `in_progress`** despite all SEE substages emitting terminal receipts. State-machine bug: progressive dispatch flips reason/procure to done but the see flag isn't being updated when substages all return SUCCEEDED_WITH_ZERO_OUTPUT. Likely the controller only flips see→done on `SUCCEEDED` not on the new variant. Patch in server-side stage tracker needed (FU-stage-progress-zero-output).
- **10 `blueprint.cascade.discipline` receipts stuck PENDING** since 19:21:22 — those orphan PENDING receipts should either complete or get superseded. Track B Pass 1.C asserted persistence on the new SUCCEEDED variants but didn't clean these up.
- **Project `address` is null** — UI allowed upload without filling the address field. Either make address required at upload OR add a backfill prompt after ingest completes.

## What's still safe-to-run

The autonomous monitoring loop is still active and watching for new uploads. If you drop the next PDF (e.g., `29187P-ES (3).pdf` — Mangonia electrical sub-bundle), we'll get a much better Drew run because MEP plans DO have symbol legends and Wave 11g-v2 was trained on them. That comparison will be very useful evidence.

## Files committed tonight

- `eb7bbce` + `6e53073` — Track B Pass 1.C (persistence-asserted receipts)
- `14a097d` + `06c90c7` — Track B Pass 2.B (page parallelism)
- `374fa8a` — Track B Pass 3.A (sweeper heartbeat)
- `dae4a3f` — Loop pause + Azure rotation completion docs
- (this brief) — overnight audit

## Recommended next moves (your call)

1. **Drop `29187P-ES (3).pdf` next** to validate Drew works on MEP/electrical (companion Mangonia file). Strong A/B vs civil run.
2. **File `FU-stage-progress-zero-output`** to patch the see-flag flip when substages return SUCCEEDED_WITH_ZERO_OUTPUT.
3. **Decide on civil-discipline strategy** — does v9 Wave 20.6 (Construction Semantic Compiler) need a civil-specific track, or do we explicitly punt on civil/landscape for the Founder Quarter and focus on MEP + architectural?

Sleep well.

---

## ADDENDUM — 3-discipline matrix completed 2026-05-26 ~20:50 UTC

User re-uploaded both metal-building (dedup hit on prior project `86851603`) and residential farmhouse (new project `5373b87e`). Combined with civil from earlier, we now have a complete discipline matrix:

| Discipline | Project | Materials | Assemblies | Quantities | Symbols | Primitives | Legends | Edges | Missing |
|---|---|---|---|---|---|---|---|---|---|
| Civil (21030 signed) | `73d70ed2` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 16 |
| Metal Building (29187P-ES) | `86851603` | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 36 |
| Residential (farmhouse) | `5373b87e` | **12** | **14** | **2** | 0 | 0 | 0 | 0 | 117 |
| MEP Gulfstream baseline | `7a93d144` | full | full | full | 234+ | yes | yes | yes | — |

### What this proves

**Drew has two extraction paths and they fail independently:**

1. **Text path** (Azure OCR → REASON LLM → schedules/notes → typed assemblies + materials) → **discipline-general**. Output scales with text richness in the PDF, not with discipline match.
2. **Vision path** (Wave 11g-v2 motifs → legend graphs → resolved symbols → semantic edges) → **MEP-specialized**. Zero output on every non-MEP corpus regardless of text richness.

### Concrete residential output Drew produced (no Wave 20.6 needed)

**Assemblies typified — full multi-discipline coverage:**
- Electrical: `electrical_branch_circuit_20a_120v`, `electrical_light_fixture_recessed_led`, `electrical_receptacle_duplex`, `electrical_receptacle_gfci`
- Plumbing: `plumbing_fixture_lav`, `plumbing_fixture_shower`, `plumbing_fixture_sink`, `plumbing_fixture_wc`, `plumbing_water_heater_electric`
- HVAC: `hvac_ahu`
- Walls: `wall_partition_gwb_both_sides`, `wall_exterior_brick_veneer`
- Finishes: `floor_finish_porcelain_tile`
- General: `general_demo`

**Materials with QUANTITIES** (first time on non-MEP corpus):
- `Plumbing — Shower fixture: 1.0 each`
- `Plumbing — Water closet fixture: 5.0 each`

Plus 10 more typed materials awaiting quantification.

### Reordered priority stack

1. **Ship text-path-only takeoff v1** — residential contractors get real value from the existing pipeline TODAY. Surface assemblies/materials/quantities Drew already extracts on the Scope/Takeoff/Materials tabs even when SEE produces zero.
2. **Fix quantity extraction on the other 10 typed materials** — Drew named them all correctly but only counted 2. The remaining 10 have schedules with quantities in the OCR; the LLM extraction prompt needs a quantity-extraction pass.
3. **Patch `stage_progress.see` controller** — currently flips to `failed` (civil) or stays `not_started` (residential) when substages are all `SUCCEEDED_WITH_ZERO_OUTPUT`. Add a `skipped_empty_by_design` state so the UI doesn't show false failure on residential takeoffs.
4. **Wave 20.6 Construction Semantic Compiler — DEFERRED in priority** — it's the path to MEP-rich precision (5,000+ symbols on Gulfstream) but not the path to "any blueprint". Text path already serves the long tail of residential/architectural at lower precision but real value.

### Updated answer to "What classes of construction documents can Drew actually read today?"

- **Civil engineering**: NO useful output (continuous linework, no parseable schedules)
- **Metal building structural**: minimal (2 components, no quantities)
- **Residential architectural**: **REAL TAKEOFF VALUE** (14 assemblies across 4 disciplines, 12 materials, 2 counted) — production-ready for v1 if we wire the existing tabs to surface it
- **MEP commercial**: full graph + symbols + quantities (Gulfstream baseline)

Drew is functional on 2 of 4 discipline classes today. That's a v1-shippable claim, narrower than "any blueprint" but real and customer-deliverable.
