---
name: drew-tariff-rules
description: Section 232 + softwood lumber HTS codes and trigger logic for blueprint material tariff classification.
version: 1.0.0
last_updated: 2026-05-17
status: active
---

# Drew Tariff Rules

## Overview

Aspire's PROCURE stage flags tariff exposure on material line items **before** procurement requests leave the platform. Most estimators skip this step entirely — contractors get blindsided at the lumberyard or steel distributor when import surcharges appear on invoices. Aspire's wedge is surfacing this exposure in the story, with estimated dollar impact, the moment materials are derived from the blueprints.

The moment a tariff flag is set on any material line, the project story gets a **"Tariff Exposure"** callout section with:
- Which materials are affected
- The governing tariff rate (%)
- An estimated dollar impact ($) based on material quantity × unit cost × tariff rate
- The regulatory authority (Section 232 / USITC CVD+AD ruling)

---

## Section 232 — Steel (50% tariff)

**Legal authority:** Section 232 of the Trade Expansion Act of 1962. Presidential Proclamations 9705 (March 2018), 9740 (April 2018), and subsequent modifications. As of 2026, the effective rate on steel articles from most non-exempt countries is **50%** (raised from 25% via 2025 Proclamation).

**Governing agency:** U.S. Department of Commerce / U.S. Customs and Border Protection.

**HTS chapters covered:**

| Chapter | Description | Common construction materials |
|---------|-------------|-------------------------------|
| 72.xx | Iron and steel (primary forms) | Steel billets, ingots, slabs |
| 7213 | Wire rod in coils | Concrete rebar feedstock |
| 7214 | Other bars and rods (not further worked) | Structural rebar |
| 7216 | Angles, shapes, sections | Structural beams, channels, angles |
| 7217 | Wire of iron or non-alloy steel | Wire mesh, wire lath |
| 72.19, 72.20 | Flat-rolled stainless steel | Stainless flashing, trim |
| 73.xx | Articles of iron or steel | Fabricated steel articles |
| 7301 | Sheet piling | Sheeting for excavation |
| 7304–7306 | Tubes, pipes, hollow profiles | Steel conduit, structural pipe, pipe piles |
| 7308 | Structures and parts of structures | Steel trusses, decking, grating, stairs |
| 7312 | Stranded wire, cables, ropes | Wire rope, guy wire |
| 7317 | Nails, tacks, staples | Structural nails, framing fasteners |
| 7318 | Screws, bolts, nuts, washers | Structural fasteners |

**Common construction materials subject to Section 232 steel tariff:**
- Concrete rebar (deformed bar, rod, #3–#11)
- Structural steel shapes (W-beams, S-beams, channels, angles, HSS)
- Steel decking (composite, roof, form deck)
- Steel joists and joist girders (open-web, longspan)
- Galvanized corrugated roofing / siding panels
- Galvanized HVAC ductwork (rectangular, round, flat oval)
- Steel pipe and tubing (structural, mechanical, Schedule 40/80)
- Steel wire mesh / welded wire reinforcement (WWR)
- Metal stud framing (light-gauge, cold-formed — NOTE: typically exempt as domestic, check origin)
- Steel conduit (rigid metal conduit / RMC, intermediate metal conduit / IMC)
- Threaded rod, anchor bolts, structural bolts
- Steel grating, bar grating, safety grating
- Steel hangers, beam clamps, pipe supports (Unistrut, Kindorf)

---

## Section 232 — Aluminum (50% tariff)

**Legal authority:** Section 232, Presidential Proclamation 9704 (March 2018) and subsequent modifications. As of 2026, effective rate is **50%** on aluminum articles from most non-exempt countries.

**HTS chapters covered:**

| Chapter | Description | Common construction materials |
|---------|-------------|-------------------------------|
| 76.xx | Aluminum and articles thereof | All fabricated aluminum products |
| 7604 | Aluminum bars, rods, profiles | Aluminum framing extrusions |
| 7605 | Aluminum wire | Aluminum service entrance cable feedstock |
| 7606, 7607 | Aluminum plates, sheets, strip, foil | Aluminum flashing, roofing panels |
| 7608, 7609 | Aluminum tubes and pipes, fittings | Aluminum conduit, pipe |
| 7610 | Aluminum structures and parts | Curtain wall, window frames, storefronts |
| 7611–7616 | Other aluminum articles | Handrails, ladders, extrusions |

**Common construction materials subject to Section 232 aluminum tariff:**
- Aluminum storefront systems (thermally broken frames, glazing frames)
- Aluminum curtain wall systems
- Aluminum window framing and sashes
- Aluminum door frames and thresholds
- Electrical metallic tubing — aluminum (EMT-Al)
- Aluminum service entrance cable (SEA/SER — aluminum conductors)
- Aluminum wire and cable (feeders, branch circuit in commercial/industrial)
- Aluminum conduit (rigid aluminum conduit / RAC)
- Aluminum roofing panels (standing seam, through-fastened)
- Aluminum flashing and trim
- Aluminum handrails and guardrails
- Aluminum composite panels (ACP) — Alucobond-type cladding
- Aluminum louvers and sun shades
- Aluminum ladder cable tray

---

## Canadian Softwood Lumber (35.2% combined tariff)

**Legal authority:** U.S. International Trade Commission (USITC) Combined CVD (Countervailing Duty) + AD (Anti-Dumping) order on softwood lumber from Canada. As of the 2026 USITC ruling, the combined effective rate is **35.2%** (CVD approximately 17.9% + AD approximately 17.3%, applied on cost-inclusive import value).

**Governing agencies:** U.S. Department of Commerce (rate-setting), U.S. Customs and Border Protection (collection).

**HTS chapters covered:**

| Chapter | Description | Common construction materials |
|---------|-------------|-------------------------------|
| 4407 | Wood sawn or chipped lengthwise, thickness > 6mm | Dimensional lumber, timbers |
| 4407.11 | Coniferous (softwood) — pine, fir, spruce | SPF framing lumber, 2x4, 2x6, etc. |
| 4409 | Wood continuously shaped along edges/faces | Moldings, T&G flooring, shiplap |
| 4411 | Fiberboard of wood (MDF and similar) | MDF panels (some Canadian origin) |
| 4412 | Plywood, veneered panels | Structural plywood |
| 4418 | Builders' joinery and carpentry of wood | I-joists, LVL, headers |

**Common construction materials subject to softwood lumber CVD+AD:**
- Dimensional framing lumber (2x4, 2x6, 2x8, 2x10, 2x12 SPF, DF, HF)
- Structural lumber (Douglas Fir, Hem-Fir, Spruce-Pine-Fir / SPF)
- Plywood (structural, sheathing — CDX, OSB/structural panels often domestic but verify)
- OSB (oriented strand board) — Canadian-origin panels
- LVL (laminated veneer lumber)
- LSL (laminated strand lumber)
- PSL (parallel strand lumber)
- Wood I-joists (TJI or equivalent — flange is typically softwood lumber)
- Glulam beams (Canadian origin)
- Headers and engineered wood beams with softwood components
- Roof sheathing panels (Canadian-origin)
- Wall sheathing (Canadian-origin)
- Exterior deck boards (softwood species, Canadian mill)

**Note on OSB:** Many OSB plants are located in the U.S. (LP, Huber, Tolko U.S. facilities). The tariff applies only to Canadian-origin panels. When Drew flags OSB, the receipt notes "Canadian origin unconfirmed — verify with supplier." In practice, estimators should confirm mill origin when ordering.

---

## Trigger Logic

### How Drew Applies These Rules

Drew's tariff engine pattern-matches the `line_item` text field from `blueprint_materials` against category keyword sets. Matching is **case-insensitive** and runs in priority order: **steel → aluminum → softwood → none**.

The first matching category wins. If a line item contains both "aluminum conduit" and "steel support", the steel rule wins because it is checked first (more conservative; contractor can override).

### Steel keyword set (Section 232 — 50%)

Primary triggers: `rebar`, `deformed bar`, `reinforcing bar`, `#4 bar`, `#5 bar`, `#6 bar`, `structural steel`, `wide flange`, `w-beam`, `s-beam`, `hss`, `steel joist`, `open web joist`, `owj`, `lh series`, `dlh series`, `steel decking`, `composite deck`, `form deck`, `roof deck`, `steel stud`, `metal stud`, `cold-formed steel`, `cfs`, `galvanized duct`, `galvanized ductwork`, `galvanized steel`, `galvanized sheet`, `steel pipe`, `schedule 40`, `schedule 80`, `black iron pipe`, `rigid metal conduit`, `rmc`, `intermediate metal conduit`, `imc`, `wire mesh`, `welded wire reinforcement`, `wwr`, `wire lath`, `metal lath`, `steel grating`, `bar grating`, `unistrut`, `strut channel`, `threaded rod`, `anchor bolt`, `structural bolt`, `a325`, `a490`, `huck bolt`, `pipe pile`, `h-pile`, `steel sheet pile`, `steel handrail`, `steel guardrail`, `cable tray` (when not specified as aluminum), `steel beam`, `steel column`, `steel plate`, `steel angle`, `steel channel`, `steel tube`, `hss tube`, `corrugated metal`, `metal roofing` (when not specified as aluminum)

### Aluminum keyword set (Section 232 — 50%)

Primary triggers: `aluminum storefront`, `aluminium storefront`, `aluminum curtain wall`, `aluminium curtain wall`, `aluminum window`, `aluminium window`, `aluminum door`, `aluminium door`, `aluminum frame`, `aluminium frame`, `emt aluminum`, `aluminum emt`, `aluminum conduit`, `aluminium conduit`, `rigid aluminum conduit`, `rac`, `aluminum wire`, `aluminium wire`, `aluminum cable`, `aluminium cable`, `aluminum conductor`, `aluminium conductor`, `service entrance aluminum`, `sea cable`, `ser aluminum`, `aluminum roofing`, `aluminium roofing`, `aluminum panel`, `aluminium panel`, `acp panel`, `aluminum composite`, `aluminum flashing`, `aluminium flashing`, `aluminum handrail`, `aluminium handrail`, `aluminum guardrail`, `aluminium guardrail`, `aluminum louver`, `aluminium louver`, `aluminum ladder`, `aluminium ladder`, `aluminum coping`, `aluminium coping`, `aluminum soffit`, `aluminium soffit`

### Softwood lumber keyword set (CVD+AD — 35.2%)

Primary triggers: `framing lumber`, `dimensional lumber`, `softwood lumber`, `spf`, `spruce-pine-fir`, `douglas fir`, `hem-fir`, `hemlock-fir`, `2x4`, `2x6`, `2x8`, `2x10`, `2x12`, `2 x 4`, `2 x 6`, `2 x 8`, `2 x 10`, `2 x 12`, `wood stud`, `lumber stud`, `plate lumber`, `sill plate`, `top plate`, `bottom plate`, `header lumber`, `roof rafter`, `ceiling joist`, `floor joist`, `ridge board`, `hip rafter`, `valley rafter`, `blocking lumber`, `bridging lumber`, `ledger board`, `plywood sheathing`, `structural plywood`, `cdx plywood`, `osb sheathing`, `osb panel`, `oriented strand board`, `lvl`, `laminated veneer lumber`, `lsl`, `laminated strand lumber`, `psl`, `parallel strand lumber`, `wood i-joist`, `tji`, `glulam`, `glue-laminated`, `engineered lumber`, `engineered wood`, `wood beam`, `wood header`, `wood nailer`, `blocking wood`, `ledger wood`, `deck board` (softwood species context), `wood decking`

### Output enum

```
section_232_steel     → 50.0% tariff
section_232_aluminum  → 50.0% tariff
softwood_lumber       → 35.2% tariff
none                  → 0.0% tariff
```

### Estimated dollar impact calculation

```
tariff_exposure_usd = quantity × unit_cost_usd × (tariff_rate / 100)
```

Where:
- `quantity` is the `blueprint_materials.quantity` field
- `unit_cost_usd` is derived from supplier price lookup (0.0 if no price available yet)
- `tariff_rate` is `estimate_tariff_impact_pct(flag)`

If no unit cost is available at PROCURE time, `tariff_exposure_usd` is set to `null` and the story callout notes "tariff impact dollar amount pending supplier pricing."

### Story callout format

When `tariff_flagged_count > 0`, Drew appends to the project story:

```
## ⚠ Tariff Exposure Alert
This project has [N] material line items subject to U.S. import tariffs.
- Section 232 Steel (50%): [count] items
- Section 232 Aluminum (50%): [count] items
- Canadian Softwood Lumber (35.2% CVD+AD): [count] items
Estimated tariff surcharge impact: $[total] (pending supplier pricing where noted).
Review your supplier quotes carefully — tariff costs are typically NOT included in
standard material pricing from domestic distributors when quoting imported stock.
```
