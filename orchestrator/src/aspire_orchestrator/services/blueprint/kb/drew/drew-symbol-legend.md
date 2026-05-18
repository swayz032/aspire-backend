---
name: drew-symbol-legend
description: Construction symbols mapped to YOLO/COCO classes for Drew SEE Wave 3.
version: 1.0.0
last_updated: 2026-05-17
status: active
---

# Drew — Construction Symbol Legend (v1 reference)

Used by Stage 4 REASON to interpret SEE-stage symbol detections AND by future
Wave 10 fine-tune curation. v1 SEE uses generic YOLOv11 + a small COCO→Aspire
class map (see `config/pack_policies/drew/symbol_class_map.yaml`), so most
sheets will produce **few or zero** matches here. That's expected. REASON
treats SEE output as a hint; OCR text is the dominant signal.

Each entry below documents what the symbol LOOKS like on a sheet, its rough
pixel footprint at 200 DPI, and the discipline that emits it. Wave 10 will
fine-tune YOLO weights directly against these geometric primitives.

---

## Architectural — Doors / Windows

### door_single
- **Visual**: 90° quarter-arc swing + jamb tick on each side.
- **Footprint @ 200 DPI**: ~600 px × 600 px (3" × 3" paper).
- **Aspect**: ~1:1.
- **Closest COCO class (v1, lossy)**: none.
- **Discipline**: A (Architectural).

### door_double
- **Visual**: Two facing quarter-arcs sharing a center jamb.
- **Footprint**: ~1200 px × 600 px.
- **Aspect**: ~2:1.

### door_sliding
- **Visual**: Two parallel slabs offset horizontally with arrow.
- **Footprint**: ~800 px × 300 px.

### window_fixed
- **Visual**: Hollow rectangle inside wall hatching, no swing arc.
- **Footprint**: ~600 px × 100 px (3" × 0.5").

### window_casement
- **Visual**: Hollow rectangle + diagonal hinge indicator.

---

## Plumbing

### plumbing_wc (water closet / toilet)
- **Visual**: Oval bowl + smaller rectangle (tank) attached.
- **Footprint**: ~500 px × 400 px.
- **Discipline**: P.

### plumbing_lavatory
- **Visual**: Rounded rectangle with small inner circle (drain).
- **Footprint**: ~400 px × 300 px.

### plumbing_floor_drain
- **Visual**: Small circle with cross or grid hatching inside.
- **Footprint**: ~100 px × 100 px.
- **Closest COCO class**: `clock` (round shape) — routed to `circular_callout`.

### plumbing_riser_symbol
- **Visual**: Small circle with arrow indicating up/down flow.

---

## Electrical

### outlet_duplex
- **Visual**: Small circle with two parallel slot lines and a hash.
- **Footprint**: ~120 px × 120 px.
- **Discipline**: E.

### outlet_gfci
- **Visual**: Same as duplex with "GFCI" or "GFI" text label.

### switch_single_pole
- **Visual**: "S" letter or small square with diagonal line.
- **Footprint**: ~100 px × 100 px.

### panel_callout
- **Visual**: Rectangular block (panel schedule) with grid of cells.
- **Footprint**: ~2000 px × 1500 px (large block).
- **Closest COCO class**: `tv` / `laptop` → `rectangular_block`.

### light_ceiling
- **Visual**: Circle with cross-hairs (×) inside.
- **Footprint**: ~150 px × 150 px.

### light_can (recessed)
- **Visual**: Plain circle, often filled or dotted.
- **Footprint**: ~100 px × 100 px.

---

## Structural

### column_callout
- **Visual**: Filled square or hatched square at column grid intersection.
- **Footprint**: ~80 px × 80 px.
- **Discipline**: S.

### beam_dim_line
- **Visual**: Long horizontal/vertical line with dimension text and arrows.

### footing_detail_bubble
- **Visual**: Circle with letter+number inside (e.g., "F-1"), pointing arrow.
- **Closest COCO class**: `clock` → `circular_callout`.

---

## Title-block / Reference

### detail_callout
- **Visual**: Hexagon or circle-with-cut with detail number + sheet number.
- **Closest COCO class**: `stop sign` → `detail_callout`.

### scale_bar
- **Visual**: Horizontal bar with alternating filled/unfilled segments and
  numeric tick labels. Bottom-right corner of sheet, typically.
- **Footprint**: ~600 px × 30 px (very high aspect ratio).
- **Note**: scale_calibrator.py uses contour-aspect-ratio (8:1 to 40:1) to
  find this — NOT YOLO.

### engineer_seal
- **Visual**: Round embossed stamp with engineer name, license number,
  and state ring text. Often signed across the seal.
- **Footprint**: 300–700 px diameter (1.5"–3.5" at 200 DPI).
- **Note**: seal_detector.py uses HoughCircles + title-block region crop —
  NOT YOLO.

---

## v1 detection expectations (honest)

On any given construction sheet, generic YOLOv11 will produce typically 0–8
boxes, with COCO classes like `clock`, `tv`, `book`, occasionally `stop sign`.
Confidence floor is 0.70, so most pass-through detections are filtered out.

This is **fine for Wave 3** — the SEE stage's job is to produce a symbol
grid with confidence scores; REASON cross-references against OCR text. The
real symbol detection lives in Wave 10 fine-tune.
