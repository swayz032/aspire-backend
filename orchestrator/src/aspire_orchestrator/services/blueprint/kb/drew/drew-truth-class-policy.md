---
name: drew-truth-class-policy
description: How to assign each truth class plus confidence floors and demotion rules.
version: 1.0.0
last_updated: 2026-05-17
status: active
---

# Drew Truth Class Policy

Every fact Drew emits — in the phased story, in assembly quantities, in material lines — must carry
a `truth` tag. This tag tells the contractor exactly how confident Drew is and what the source was.
No fact is ever emitted without a truth tag. A fact that cannot reach its confidence floor becomes a
`missing_input` instead.

---

## Truth Classes

### `observed`
Drew literally read this value from OCR text on a sheet, or it appears verbatim in the sheet title
block, note cloud, or schedule.

**Examples:**
- Sheet A-1 title block reads "DEMISING WALL — 8 FT, GWB-MR" → wall height is `observed`.
- Sheet P-2 notes read "3-inch DWV line below slab" → pipe size is `observed`.
- Sheet title reads "ADDENDUM 1 — SUPERSEDES A-3" → revision relationship is `observed`.

**Confidence:** Not applicable — observed facts are taken at face value. If the OCR text is
ambiguous (e.g., partial characters, uncertain reading), Drew should note the ambiguity and emit the
fact as `derived` at lower confidence instead.

**Rule:** Never claim `observed` for a value that was inferred, calculated, or assumed — even if
the inference is obvious. Reserve `observed` for facts that could be read aloud verbatim from the
paper.

---

### `derived`
Drew computed this value from one or more observed facts, using a deterministic calculation (not
LLM inference).

**Examples:**
- Linear feet of demising wall = polygon perimeter on floor plan × `scale_factor` from
  `blueprint_sheets.scale`. This is `derived` because it uses math on an observed scale.
- Area of a room = observed dimensions (width × length from A sheet notes).
- Sheet is superseded = derived from `supersedes_id` relationship in the DB (set by CLASSIFY stage).

**Confidence floor: 0.85.** If Drew cannot reach 0.85 on a derived fact (e.g., scale was not
calibrated, only one dimension was readable), demote to `missing_input`.

**Rule:** Derived facts require at least two observable anchors: a measured value AND a confirmed
scale. If either anchor is missing or low-confidence, the derived quantity must not be emitted as a
line item.

---

### `assumed`
Drew used LLM inference to fill a gap — drawing on construction norms, discipline context, and
project type — when the blueprints did not explicitly state the value.

**Examples:**
- No sheet specifies ceiling height, but the project is a commercial TI. Drew assumes 9 ft based on
  commercial-lite norm: `assumed 0.72`.
- Electrical panel amperage not listed in OCR text, but the load schedule suggests 200A service:
  `assumed 0.71`.
- Roofing material not specified by brand, but spec section says "60-mil TPO membrane": `assumed
  0.80` (spec exists but brand ambiguity remains).

**Confidence floor: 0.70.** If Drew's inference confidence is below 0.70, the item must not be
emitted as a material line or assembly. Emit it as a `missing_input` instead.

**Important:** Assumed facts must always state the reasoning concisely in the story narrative (e.g.,
"ceiling height likely 9 ft based on commercial-lite norm — no sheet specifies"). Drew never hides
an assumption.

---

### `field_confirmed`
A contractor has reviewed this fact in the Aspire app and confirmed it is correct. This class is set
by `useBlueprintActions.confirmAssumption` on the frontend — Drew does not set it.

**Use:** Drew reads `field_confirmed` facts from prior case-pack hints and treats them as
equivalent to `observed` for the purposes of new story generation.

---

### `vendor_confirmed`
A supplier has confirmed a quantity or specification via an RFQ response. Set by Wave 5 PROCURE
stage — Drew does not set this class during REASON.

---

### `permit_confirmed`
The fact has been cross-referenced against municipal permit data. Deferred to a future version (v2).
Drew does not set this class.

---

## Confidence Floors Summary

| Truth class       | Minimum confidence to emit | Below floor → action          |
|-------------------|---------------------------|-------------------------------|
| `observed`        | N/A (literal read)        | Flag OCR ambiguity in story   |
| `derived`         | 0.85                      | Emit as `missing_input`       |
| `assumed`         | 0.70                      | Emit as `missing_input`       |
| `field_confirmed` | N/A (human confirmed)     | Treat as observed             |
| `vendor_confirmed`| N/A (Wave 5)              | N/A for REASON                |
| `permit_confirmed`| N/A (v2 deferral)         | N/A for REASON                |

---

## Demotion to `missing_input`

When a fact cannot meet its confidence floor, Drew emits a `missing_input` row instead of a
low-confidence fact. The `missing_input` describes:
- What information is needed (e.g., "Ceiling height not specified on any sheet").
- Where the contractor can find it (e.g., "Check title-block notes on A-1 or confirm on site").
- Why Drew could not derive it (e.g., "Scale not calibrated — no scale bar detected on these
  sheets").

**Drew never emits a material quantity as `missing_input` with a guessed value.** The
`missing_input` has no quantity — it is a request for information, not a placeholder estimate.

---

## Seal-Detected Trust Upgrade

When `blueprint_sheets.seal_detected = true` for a sheet, facts derived or assumed from that sheet
receive a **+0.05 confidence boost** before the floor check. Rationale: a licensed professional
engineer's seal is evidence that the documented values have been reviewed for accuracy.

**Example:** A derived wall height calculation from a sealed structural sheet would have its
confidence raised from 0.82 to 0.87, pushing it above the 0.85 floor and allowing it to be emitted
as `derived` rather than demoted to `missing_input`.

The boost applies to `derived` and `assumed` facts sourced from a sealed sheet. It does not apply
to `observed` facts (which are already taken at face value).
