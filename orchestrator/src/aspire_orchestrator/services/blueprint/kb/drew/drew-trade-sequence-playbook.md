---
name: drew-trade-sequence-playbook
description: Standard construction sequences used during Stage 4 REASON to phase the story.
version: 1.0.0
last_updated: 2026-05-17
status: active
---

# Drew Trade Sequence Playbook

Drew uses this reference during Stage 4 REASON to order the phased story. Every project type has a
canonical trade sequence. Drew assigns each derived fact and assembly to a phase, then renders the
story in phase order so a contractor can read it as a logical work plan — not an alphabetical dump.

**Key rule:** If the discipline mix does not match a known project type, default to the TI Buildout
sequence and flag the project type as a `missing_input` so the contractor can confirm.

---

## TI Buildout (Tenant Improvement / Interior Fit-Out)

The most common job type in the Aspire ICP. A new tenant occupies an existing commercial shell and
builds out the interior. The shell (roof, exterior skin, structural frame, main electrical service,
main plumbing risers) is already in place.

**Typical discipline mix:** A (dominant), E, P, M, FP (if sprinkler is modified). Rarely S or C.

### Phase sequence

1. **Demo** — Existing partition removal, ceiling grid tear-down, floor covering removal. Scope
   driven by A sheets. Demo drawings usually carry a revision cloud if the space was previously
   occupied. Drew should flag demo items as `observed` when demo notes appear in OCR text.

2. **MEP Rough** — Electrical conduit/feeders, plumbing rough-in, HVAC ductwork backbone and
   equipment curbs are all installed before walls close. This is the critical coordination phase:
   electrical rough must complete before framing closes walls; plumbing rough must be inspected
   before slab is poured (if applicable). Drawn on E, P, M rough sheets.

3. **Framing / Drywall** — Metal stud framing of new partitions, blocking, drywall hang, taping
   and finishing. Scope from A partition plans and wall type schedules. Ceiling grid installed after
   drywall. Interdependency: framing cannot start until MEP rough is signed off.

4. **HVAC Trim** — VAV boxes, diffusers, grilles, exhaust fans, thermostats, controls wiring.
   Requires ceiling grid in place. HVAC trim typically follows electrical rough and framing.

5. **Electrical Trim** — Devices (receptacles, switches, GFCI outlets), luminaires, panel trim,
   fire alarm devices. Interdependency: walls must be painted before device covers are installed.

6. **Plumbing Fixtures** — Water closets, lavatories, sinks, water heater trim, floor drain covers.
   Drawn on P fixture schedules.

7. **Finishes** — Flooring (LVT, carpet, ceramic tile), painting, ceiling tile, millwork, casework,
   door hardware, glass partitions. Scope from room finish schedules and door schedules in A sheets.

8. **Punch / Commissioning** — Final inspections, HVAC balancing, electrical testing, fire alarm
   acceptance test, punch list walk with GC and tenant.

---

## Ground-Up Commercial-Lite

New construction of a commercial building under roughly 20,000 SF (retail pad, small office,
medical office, quick-service restaurant shell). Full discipline set is present.

**Typical discipline mix:** A, S, C, E, P, M, FP, sometimes L.

### Phase sequence

1. **Site Prep** — Clearing, grubbing, rough grading, erosion control BMP installation. Scope from
   C grading and erosion sheets. Drew should flag SWPPP notes as `observed`.

2. **Underground Utilities** — Sanitary sewer, domestic water, storm drain, electrical conduit
   sleeves, gas line — all installed and inspected before foundations are poured. Drawn on C utility
   sheets and P/E underground plans.

3. **Foundations** — Spread footings, grade beams, slab-on-grade (or elevated slab if applicable).
   Structural engineer's seal on S foundation sheets is a `seal_detected` trust upgrade trigger.
   Interdependency: soils report (geotechnical reference) must be reviewed before concrete is
   placed.

4. **Vertical Structure** — Structural steel erection or CMU/tilt-up panel placement. Scope from S
   framing plans and connection details. Anchor bolts set during foundations phase.

5. **Roof Envelope** — Roof deck, insulation, waterproofing membrane, metal flashing. Often driven
   by metal deck schedule in S sheets and roofing spec in Specs. Roofing material tariff exposure
   applies here (see tariff rules).

6. **Exterior Skin** — Storefront glazing, exterior cladding, doors, overhead doors, parapet caps.
   Scope from A exterior elevations and door schedules.

7. **MEP Rough** — Interior conduit, ductwork, plumbing rough-in. Same rough-before-close rule as
   TI. M/E/P rough sheets coordinate overhead congestion.

8. **Interior Framing** — Metal stud partitions, drywall, insulation, soffits. Scope from A
   interior elevations and wall sections.

9. **MEP Trim** — Devices, fixtures, equipment connections. Same sequence as TI trim phases.

10. **Finishes** — Flooring, painting, millwork, casework, signage. Scope from room finish
    schedules.

11. **Commissioning** — HVAC test and balance, fire alarm acceptance, final utility connections,
    Certificate of Occupancy walk.

---

## Residential Remodel

Renovation of an occupied single-family or multi-family residential unit. Scope is driven by
the affected areas (kitchen, bath, addition, whole-house). Residential permits are typically
smaller sets: A + E + P; mechanical is sometimes absent if existing HVAC is reused.

**Typical discipline mix:** A (dominant), E, P. M only if HVAC work is in scope.

### Phase sequence

Residential remodels are scoped by affected area. Drew should identify the primary affected areas
from A sheet titles and structure the story per area.

**Per affected area (kitchen, bath, addition):**

1. **Demo** — Cabinet removal, flooring tear-out, fixture removal, wall opening. Scope from A demo
   plan notes.

2. **MEP Rough (affected area)** — Electrical rough for new circuits, plumbing rough-in for new
   fixture locations, HVAC rough if adding supply/return. Interdependency: rough must be inspected
   before walls are closed (code-required rough-in inspection).

3. **Drywall** — Hang, tape, texture. Scope from A wall types and notes.

4. **Finishes** — Paint, flooring (tile, hardwood, LVT), millwork, cabinets, countertops. Scope
   from finish notes and specifications.

5. **Fixtures** — Plumbing fixtures (faucets, toilets, sinks, shower valves), electrical
   devices and luminaires, appliances. Scope from P and E fixture schedules.

**Whole-house / large addition:** Add a Structural phase (beam and post installation, shear wall
sheathing) between Demo and MEP Rough.

---

## Roofing (Replacement or New)

Roofing is a stand-alone scope — typically a single-discipline job with A sheets (roof plan) and
sometimes S sheets (structural deck inspection notes). Tariff exposure is HIGH: steel decking and
steel fasteners are subject to Section 232 tariffs; TPO membrane may have tariff exposure if
imported.

**Typical discipline mix:** A (roof plan), sometimes S (deck notes or structural notes).

### Phase sequence

1. **Tear-Off (if replacement)** — Remove existing membrane layers down to structural deck.
   Identify scope from A demo notes ("full tear-off" vs "overlay" vs "recover"). Drew should flag
   whether the project is a full tear-off or recover as `observed` (from OCR) or `assumed` (inferred
   from sheet title) depending on source.

2. **Deck Inspection** — Identify rotted, corroded, or damaged deck panels. Scope from S deck
   inspection notes. Any damaged deck repair quantity is `missing_input` unless notes specify.

3. **Underlayment / Vapor Retarder** — Base sheet, insulation board (polyiso or EPS), cover board.
   Scope from roofing spec and A roof plan material legend.

4. **Flashing** — Edge metal, parapet cap flashing, curb flashing, pipe penetration flashings.
   Scope from A roof plan and detail sheets. Flashing metal is typically galvanized steel or
   aluminum — tariff exposure applies.

5. **Primary Roof Material** — TPO/EPDM membrane, built-up roofing (BUR), metal standing seam, or
   tile. Scope from A roof plan and roofing spec. This is the highest-cost line item on a roofing
   job.

6. **Trim / Accessories** — Drains, scuppers, walkway pads, equipment curbs, lightning protection
   if applicable. Scope from A roof plan.

7. **Cleanup / Final Inspection** — Debris removal, drain testing, owner walkthrough.
