---
name: drew-storytelling-examples
description: Worked examples — GAVNN Addendum 1 (canonical golden), ENG_Rev1 multi-discipline, TI buildout, roofing replacement.
version: 1.0.0
last_updated: 2026-05-17
status: active
---

# Drew Storytelling Examples

These four worked examples define Drew's expected narrative style, truth tagging, and
missing_input surfacing. The GAVNN and ENG_Rev1 examples are **canonical goldens** — the
test suite asserts against their key structural properties.

---

## Example 1: GAVNN Addendum 1 (Canonical Golden — Addenda Supersession)

### Input excerpt

**Sheets ingested:**
- Sheet A-3 (original): "FLOOR PLAN — SUITE 200, SCALE 1/4″=1′-0″, REV 0"
  - OCR excerpt: "DEMISING WALL (TYP): 3-5/8" MTL STUD, GWB EA SIDE, 10'-0" CLG HT"
  - Symbols: 3 door_single, 1 window_fixed detected
- Sheet A-3 (addendum): "ADDENDUM 1 — REPLACES SHEET A-3 REV 0 — FLOOR PLAN SUITE 200 REV 1"
  - OCR excerpt: "DEMISING WALL (TYP): 3-5/8" MTL STUD, GWB-MR EA SIDE, 10'-0" CLG HT — NOTE: GWB UPGRADE TO MOISTURE-RESISTANT PER TENANT REQUEST"
  - Symbols: 3 door_single, 2 window_fixed detected (window added in revision)
  - `seal_detected: false`, `supersedes_id: <A-3 REV 0 id>`
- Scale: 1/4"=1'-0" → 48 px/ft at 200 DPI (calibrated, confidence 0.91)
- discipline: Addenda (A-3 addendum sheet); A (original A-3)

**Symbol summary:** 3 door_single @ 0.82 mean confidence, 2 window_fixed @ 0.77 mean confidence.

### Drew's story output

```markdown
## Phase 1 — Demo

No demo scope identified on addendum sheets. The addendum adds a window to the east demising wall
(observed from sheet count change) but does not show existing conditions requiring demolition. If
existing wall openings need to be patched for the new window, confirm with GC. **Missing input:**
demo scope for new window opening.

## Phase 2 — MEP Rough

No MEP sheets in this package. HVAC, electrical, and plumbing rough-in scope is not documented
here. **Missing input:** MEP rough sheets not provided — contractor must confirm whether MEP
drawings are in a separate package or whether existing MEP is reused without modification.

## Phase 3 — Framing / Drywall

**Demising wall system (derived 0.91):** Addendum A-3 Rev 1 specifies 3-5/8″ metal stud framing
with moisture-resistant GWB on each side. Ceiling height is 10′-0″ (observed). The addendum
upgrades the original GWB specification to moisture-resistant board — this revision supersedes
sheet A-3 Rev 0. Drew is reading the addendum version only.

Scale calibrated at 1/4″=1′-0″ (confidence 0.91, text match). Perimeter of demising wall shown on
plan is approximately 84 linear feet (derived 0.91 from plan geometry × scale factor). This does
not include the two door openings (3 door_single symbols detected at mean confidence 0.82) —
deducted from linear footage.

**Assemblies:**
- Demising wall, 3-5/8″ MTL STUD / GWB-MR both sides, 10′-0″ height — 84 LF (derived 0.91)
- Door openings — 3 ea (observed, door_single × 3)
- Window openings — 2 ea (observed, window_fixed × 2 on addendum sheet)

**Note on supersession:** This story uses addendum A-3 Rev 1 as the authoritative source.
Sheet A-3 Rev 0 is superseded and excluded from takeoff.

## Phase 7 — Finishes

GWB is moisture-resistant (GWB-MR) per addendum note (observed). No finish schedule in this
package. **Missing input:** Room finish schedule not provided — floor, ceiling, and paint finishes
cannot be quantified.
```

### Why this output

**What Drew observed:**
- Wall type description with stud size and GWB specification from addendum OCR.
- Ceiling height "10′-0″" verbatim in OCR text.
- "ADDENDUM 1 — REPLACES SHEET A-3 REV 0" in sheet title block.
- 3 door_single symbols and 2 window_fixed symbols from SEE stage.

**What Drew derived (and why confidence was high):**
- 84 LF demising wall perimeter: scale was text-matched at 0.91 confidence (above 0.85 floor).
  Polygon perimeter measured from plan geometry × scale factor = derived quantity.
- Revision relationship: `supersedes_id` confirmed by CLASSIFY stage — addendum sheet links to
  original A-3 row in DB.

**What Drew assumed (and why it was reasonable):**
- No assumptions were made for quantified line items in this example. The ceiling height and wall
  spec were both observed. If ceiling height had not been stated, Drew would have assumed 10 ft for
  commercial TI at confidence 0.72.

**What became missing_input (and why):**
- Demo scope for window opening: Drew saw that a window was added in the revision (window count
  went from 1 to 2) but cannot determine whether existing framing requires demo. OCR does not
  specify. Contractor must confirm.
- MEP sheets: Not included in the package. Drew cannot estimate MEP scope from what is not there.
- Room finish schedule: Not provided. Drew cannot surface finish quantities.

---

## Example 2: ENG_Rev1 Signed Master (Mangonia Park, FL — Multi-Discipline Site Utility)

### Input excerpt

**Sheets ingested:**
- C-1: "SITE UTILITY PLAN — MANGONIA PARK FL — SCALE 1:50" — `seal_detected: true`
  - OCR excerpt: "8" PVC SANITARY SEWER, APPROX 180 LF, SLOPE 1/8" PER FOOT; 4" DOMESTIC WATER
    MAIN, APPROX 95 LF; INSTALL 4 CATCH BASINS TYPE 'D'"
- P-1: "PLUMBING PLAN — MANGONIA PARK FL" — `seal_detected: true`
  - OCR excerpt: "3" DWV CAST IRON BELOW SLAB; 1-1/2" HWS/HWR; FIXTURE UNIT COUNT: 22 FU"
- E-1: "ELECTRICAL SITE PLAN" — `seal_detected: true`
  - OCR excerpt: "200A, 120/240V SINGLE PHASE SERVICE; FEEDER: 2-#4/0 + #2 GND IN 2" CONDUIT"
- E-2: "LIGHTING PLAN" — `seal_detected: false`
  - OCR excerpt: "12 POLE-MOUNTED FIXTURES, 150W LED, PHOTOCELL CONTROLLED"
- Scale (C-1): 1:50 calibrated via text match, confidence 0.88; after seal_detected=true boost → 0.93
- discipline: C (C-1), P (P-1), E (E-1, E-2)

**Symbols summary:** 4 `circular_callout` (likely catch basins or drain callouts), 1 `panel_callout`
on E-1 at 0.75 confidence. Seal detected on C-1, P-1, E-1 (3 of 4 sheets).

### Drew's story output

```markdown
## Phase 1 — Site Prep / Underground Utilities

Three of four sheets carry an engineer's seal — facts derived from sealed sheets receive a
+0.05 confidence boost per Drew trust policy.

**Sanitary sewer (derived 0.93):** C-1 specifies 8-inch PVC sanitary sewer at approximately
180 linear feet, slope 1/8″ per foot (observed). Scale 1:50 calibrated at confidence 0.93
(text match + seal boost). Length is derived from plan annotation rather than geometric
measurement — annotation is the primary source here.

**Domestic water main (derived 0.93):** 4-inch domestic water main at approximately 95 linear
feet (observed annotation on C-1, confidence boosted to 0.93 by seal).

**Storm drainage (observed):** 4 catch basins Type D (observed directly from C-1 OCR). No
dimensions for storm pipe specified — contractor must confirm storm pipe size and length.
**Missing input:** storm drain pipe diameter and total length not specified.

## Phase 2 — Foundations / Below-Slab Plumbing

**DWV below slab (observed):** P-1 specifies 3-inch cast-iron DWV below slab (observed from
P-1 OCR, engineer-sealed sheet). Fixture unit count: 22 FU (observed). No isometric riser
diagram in this package — below-slab routing lengths are not quantifiable from plan view alone.
**Missing input:** DWV below-slab linear footage requires isometric or plan dimensions.

**Hot water supply/return (observed):** 1-1/2-inch HWS/HWR piping noted on P-1 (observed).
Length not specified. **Missing input:** HWS/HWR piping length.

## Phase 5 — Electrical Service / MEP Rough

**Service entrance (derived 0.93):** E-1 (sealed) specifies 200A, 120/240V single-phase
service. Feeder is 2 × #4/0 conductors + #2 ground in 2-inch conduit (observed). Panel
callout detected on E-1 (confidence 0.75). Feeder length from utility transformer to panel is
not dimensioned. **Missing input:** feeder run length for conduit and wire quantity takeoff.

## Phase 8 — Electrical Trim / Site Lighting

**Site lighting (derived 0.82):** E-2 (no seal, confidence not boosted) shows 12 pole-mounted
LED fixtures at 150W each with photocell control (observed from OCR). Pole spacing and conduit
routing not shown in OCR excerpt. Quantity (12 fixtures) is observed; conduit lengths are not
derivable from available data. **Missing input:** site lighting pole conduit runs — lengths
required for underground conduit takeoff.
```

### Why this output

**What Drew observed:**
- All pipe sizes, approximate lengths, fixture unit count, and service ampacity directly from OCR
  text on engineer-sealed drawings.
- Fixture and equipment counts (4 catch basins, 12 light poles) from OCR annotations.

**What Drew derived (and why confidence was high):**
- Derived quantities on sealed sheets benefited from +0.05 boost, pushing calibration-based
  derivations above the 0.85 floor. Without the boost, scale-based derivations would have
  been at 0.88 — still above floor, but the boost provides additional margin.
- The seal-detected flag on C-1, P-1, E-1 indicates licensed professional review — Drew applies
  higher trust to these sheets accordingly.

**What Drew assumed:**
- No assumptions were needed for quantified items — all pipe sizes and approximate lengths were
  observed from annotated plans.

**What became missing_input (and why):**
- Storm drain pipe spec: C-1 shows catch basin callouts but OCR did not capture pipe diameter
  or length. Drew does not invent a standard size.
- DWV below-slab lengths: P-1 shows the system exists but no plan dimensions or isometric.
- Feeder run length: E-1 shows conductor size but no plan dimension to the service point.
- Site lighting conduit: E-2 shows pole count but no conduit routing on this sheet.

---

## Example 3: TI Buildout (Synthetic)

### Input excerpt

**Sheets ingested (synthetic):**
- A-1: "DEMO PLAN — SUITE 1400" — `seal_detected: false`, scale 1/4″=1′-0″ (confidence 0.89)
  - OCR excerpt: "REMOVE EXIST. PARTITIONS (TYP SEE DEMO NOTES); PATCH FLOOR AT REMOVED WALLS;
    EXIST. SUSPENDED ACT CEILING TO REMAIN"
- A-2: "FLOOR PLAN — SUITE 1400" — `seal_detected: false`
  - OCR excerpt: "NEW PARTITION (TYP): 3-5/8" MTL STUD @ 16" OC, 5/8" GWB; (8) NEW DOORS TYPE
    'A' (SEE DOOR SCHEDULE); TOTAL SUITE AREA: 3,200 SF"
- A-3: "DOOR SCHEDULE" — `seal_detected: false`
  - OCR excerpt: "TYPE A: 3′-0″ × 8′-8″ HM FRAME / SOLID CORE WD, HARDWARE SET 2"
- E-1: "LIGHTING / POWER PLAN" — `seal_detected: false`
  - OCR excerpt: "NEW 150A PANEL 'LP-14' (SEE PANEL SCHEDULE); (34) 2×4 LED TROFFER, 3500K;
    POWER: (42) DUPLEX RECEPTACLES, CIRCUITS 20A EA"
- M-1: "HVAC PLAN" — `seal_detected: false`
  - OCR excerpt: "NEW 5-TON SPLIT SYSTEM (RTU-1); 12 VAV BOXES, SEE SCHEDULE; 24 DIFFUSERS"

**Symbol summary:** 8 door_single symbols at mean confidence 0.84, 34 light_ceiling at 0.78.

### Drew's story output

```markdown
## Phase 1 — Demo

**Partition removal (observed):** A-1 shows existing partitions to be removed throughout Suite
1400 (observed from OCR demo notes). Floor patching required at removed wall locations (observed).
Existing suspended ACT ceiling to remain — no ceiling demo scope (observed).

**Missing input:** Demo quantity (linear feet of existing partitions to remove) cannot be derived
without existing conditions dimensions on A-1. Contractor should measure or confirm from field.

## Phase 3 — Framing / Drywall

**New partition system (observed):** 3-5/8″ metal stud at 16″ OC, 5/8″ GWB per A-2 wall type
notes (observed). No moisture-resistant GWB called out — standard GWB assumed for interior
non-wet areas (assumed 0.75).

**Suite area (observed):** 3,200 SF total suite area per A-2 plan note (observed).

**New door openings (observed):** 8 doors Type A per door schedule note on A-2 (observed).
Door type: 3′-0″ × 8′-8″ HM frame, solid core wood, Hardware Set 2 per A-3 door schedule
(observed). 8 door_single symbols detected by SEE stage at mean confidence 0.84 — count matches
door schedule (corroborating).

**Assemblies:**
- New interior partition, 3-5/8″ MTL STUD / 5/8″ GWB both sides — linear footage is
  **missing input** (scale 0.89 but plan geometry not captured in this excerpt).
- New door, HM frame, 3×8.8, solid core WD, Hwd Set 2 — 8 ea (observed)

## Phase 4 — HVAC Trim

**RTU and VAV system (observed):** M-1 specifies one 5-ton rooftop unit (RTU-1) and 12 VAV boxes
(observed). 24 supply diffusers (observed). Equipment schedule referenced but not provided in OCR
excerpt — manufacturer and model are unknown. **Missing input:** RTU equipment schedule for
make/model, electrical connection size, and curb specification.

**Assemblies:**
- RTU, split system, 5-ton — 1 ea (observed)
- VAV box — 12 ea (observed)
- Supply air diffuser — 24 ea (observed)

## Phase 5 — Electrical Trim

**Panel (observed):** New 150A lighting/power panel LP-14 (observed from E-1). Panel schedule
referenced but full schedule not in OCR excerpt. **Missing input:** panel schedule for circuit
count and breaker sizing.

**Lighting (observed):** 34 × 2×4 LED troffer, 3500K (observed). 34 light_ceiling symbols
detected by SEE at mean confidence 0.78 — count matches plan note (corroborating).

**Power (observed):** 42 duplex receptacles on 20A circuits (observed from E-1 plan note).

**Assemblies:**
- Panel, 150A, 120/208V 3Ø (assumed 0.72 — voltage not stated, commercial 3Ø assumed for
  150A panel), 1 ea
- LED troffer, 2×4, 3500K — 34 ea (observed)
- Duplex receptacle, 20A — 42 ea (observed)
```

### Why this output

- Wall type, door count/spec, equipment counts all observed directly from OCR.
- Partition linear footage is missing_input because OCR captured the area note but not plan
  geometry dimensions, and scale alone cannot derive perimeter from area.
- 3-phase panel assumption at 0.72 is stated explicitly — below 0.85 but above 0.70 assumed floor.
- SEE symbol counts (doors, lights) corroborate OCR-observed counts, adding confidence.

---

## Example 4: Roofing Replacement (Synthetic — Tariff Exposure)

### Input excerpt

**Sheets ingested (synthetic):**
- A-R1: "ROOF PLAN — EXISTING CONDITIONS" — `seal_detected: false`
  - OCR excerpt: "EXISTING: 3-PLY BUR ON 20-GA METAL DECK; FULL TEAR-OFF REQUIRED; ROOF AREA:
    14,200 SF"
- A-R2: "ROOF PLAN — NEW WORK" — `seal_detected: false`, scale 1/8″=1′-0″ (confidence 0.86)
  - OCR excerpt: "NEW: 60-MIL TPO MEMBRANE FULLY ADHERED; R-25 POLYISO INSULATION 4.5" NOMINAL;
    NEW 24-GA GALV EDGE METAL 7,300 LF; (6) NEW 4" ROOF DRAINS; VERIFY DECK CONDITION PRIOR
    TO ROOFING"

**Symbol summary:** 6 circular_callout symbols (likely roof drains) at mean confidence 0.81.
Tariff exposure: galvanized steel edge metal (Section 232 steel) and 20-ga metal deck replacement
may be subject to Section 232 tariffs.

### Drew's story output

```markdown
## Phase 1 — Tear-Off

**Full tear-off (observed):** A-R1 specifies full tear-off of existing 3-ply BUR system down to
metal deck (observed). Existing deck is 20-gauge metal — deck condition must be verified after
tear-off (observed from A-R2 note "VERIFY DECK CONDITION PRIOR TO ROOFING").

**Missing input:** Deck replacement scope is unknown until tear-off. Any damaged deck panels
will be additional material and labor not quantifiable from these drawings.

**Roof area (observed):** 14,200 SF per A-R1 existing conditions note (observed).

## Phase 2 — Deck Inspection

Deck condition verification required per A-R2 note (observed). No structural notes in this
package specify expected deck replacement percentage. Drew cannot estimate deck repair scope.

**Missing input:** Deck repair allowance — contractor must inspect deck after tear-off.

## Phase 3 — Underlayment / Insulation

**Insulation (observed):** R-25 polyiso insulation, 4.5-inch nominal thickness (observed from
A-R2 OCR). Area derived from roof plan: 14,200 SF (derived 0.86 — scale 1/8″=1′-0″ at
confidence 0.86, corroborated by observed area note).

**Assembly:**
- Polyiso insulation, R-25, 4.5" nominal — 14,200 SF (derived 0.86)

## Phase 4 — Flashing

**Edge metal (observed):** 24-gauge galvanized steel edge metal, 7,300 linear feet per A-R2
(observed).

**Tariff flag:** 24-gauge galvanized steel edge metal is subject to **Section 232 steel tariff**.
Confirm current tariff rate before finalizing material pricing. See Drew tariff rules.

**Assembly:**
- Galv. steel edge metal, 24-ga — 7,300 LF (observed) [TARIFF: section_232_steel]

## Phase 5 — Primary Roof Material

**TPO membrane (observed):** 60-mil TPO, fully adhered system, per A-R2 (observed). Area:
14,200 SF (derived 0.86 from plan area note + scale).

**Assembly:**
- TPO membrane, 60-mil, fully adhered — 14,200 SF (derived 0.86)
- Polyiso insulation, R-25 (see Phase 3) — included above.

## Phase 6 — Trim / Accessories

**Roof drains (observed):** 6 × 4-inch roof drains per A-R2 OCR note (observed). 6
circular_callout symbols detected by SEE stage at mean confidence 0.81 — corroborates count.

**Assembly:**
- Roof drain, 4-inch cast iron — 6 ea (observed)
```

### Why this output

- Roof area, material specs, drain count, and edge metal footage are all observed from OCR.
- Derived quantities use the text-stated area (14,200 SF) as the anchor — this is more reliable
  than geometric measurement for a large plan at 1/8″ scale.
- Deck repair scope is correctly surfaced as missing_input — Drew never invents an allowance.
- Section 232 tariff flag is emitted on galvanized steel edge metal per tariff rules KB.
- SEE symbol count (6 circular_callouts) corroborates the observed drain count.
