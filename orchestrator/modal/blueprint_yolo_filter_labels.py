"""Aspire Drew SEE — Filter GroundingDINO auto-labels for training quality.

GroundingDINO zero-shot returns many duplicate region-scale boxes across
classes ("this is a drawing area"). For training a YOLO specialist we need
tight per-symbol boxes. This filter applies several quality gates:

  Gate 1: max bbox area — drop boxes covering > MAX_AREA_FRAC of image.
          Real symbols are small. A bbox spanning >5-10% of the page is
          almost always a "region detection", not a symbol.
  Gate 2: aspect ratio — drop extreme ratios (a `door` shouldn't be 50x1).
  Gate 3: class-NMS — when boxes from DIFFERENT classes overlap by >IOU_THR,
          keep the highest-confidence one. This kills the "same bbox for 5
          different classes" pattern.
  Gate 4: per-class NMS — standard within-class deduplication.
  Gate 5: minimum confidence — class-specific floor.

After filter, also writes a per-class summary so we can verify the
distribution is still useful for training.
"""

from __future__ import annotations

import modal

APP_NAME = "aspire-drew-yolo-filter"
app = modal.App(APP_NAME)
dataset_volume = modal.Volume.from_name("aspire-blueprint-dataset", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("numpy>=1.24.0", "Pillow>=10.0.0")
)

# Tuned for construction symbols: most are <5% of the page.
MAX_AREA_FRAC = 0.05
MIN_AREA_FRAC = 0.00005  # ~30x30 px on a 2500x2500 image — anything smaller is noise
MAX_ASPECT = 8.0         # door swing can be elongated but not 50x1
CROSS_CLASS_IOU_THR = 0.50  # if same bbox shows up for 2 classes >50% IoU, keep best
WITHIN_CLASS_IOU_THR = 0.45

# Per-class confidence floors — looser on rare classes
CONF_FLOORS = {
    "electrical_outlet": 0.35,
    "electrical_panel": 0.40,
    "electrical_switch": 0.35,
    "light_fixture": 0.35,
    "light_pole": 0.35,
    "electrical_conduit_run": 0.30,
    "plumbing_fixture_wc": 0.35,
    "plumbing_fixture_lav": 0.35,
    "plumbing_floor_drain": 0.30,
    "plumbing_riser": 0.30,
    "door": 0.35,
    "window": 0.35,
    "structural_column": 0.35,
    "rebar_callout": 0.25,
}

CLASS_NAMES = [
    "electrical_outlet", "electrical_panel", "electrical_switch",
    "light_fixture", "light_pole", "electrical_conduit_run",
    "plumbing_fixture_wc", "plumbing_fixture_lav",
    "plumbing_floor_drain", "plumbing_riser",
    "door", "window", "structural_column", "rebar_callout",
]


def _iou(a, b) -> float:
    """IoU of two YOLO bboxes (cx, cy, w, h normalized)."""
    ax1, ay1, ax2, ay2 = a[0] - a[2]/2, a[1] - a[3]/2, a[0] + a[2]/2, a[1] + a[3]/2
    bx1, by1, bx2, by2 = b[0] - b[2]/2, b[1] - b[3]/2, b[0] + b[2]/2, b[1] + b[3]/2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    aarea = (ax2 - ax1) * (ay2 - ay1)
    barea = (bx2 - bx1) * (by2 - by1)
    union = aarea + barea - inter
    return inter / union if union > 0 else 0.0


@app.function(image=image, timeout=10 * 60, volumes={"/data": dataset_volume})
def filter_labels(
    src_subdir: str = "auto_labels_v1",
    dst_subdir: str = "filtered_labels_v1",
) -> dict:
    """Apply quality gates to GroundingDINO outputs and produce training labels."""
    import json
    from pathlib import Path
    from collections import Counter

    src = Path(f"/data/{src_subdir}/labels")
    dst = Path(f"/data/{dst_subdir}/labels")
    dst.mkdir(parents=True, exist_ok=True)

    raw_count = Counter()
    kept_count = Counter()
    per_image_summary = []

    commented_files = sorted(src.glob("*.commented.txt"))
    print(f"[filter] processing {len(commented_files)} label files")

    for cf in commented_files:
        sheet = cf.stem.replace(".commented", "")
        # Parse: each line is `class_id cx cy w h  # class_name conf`
        candidates = []  # (class_id, cx, cy, w, h, conf, class_name)
        for line in cf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                left, comment = line.split("  #", 1)
                parts = left.split()
                class_id = int(parts[0])
                cx, cy, w, h = (float(x) for x in parts[1:5])
                cname, cstr = comment.strip().rsplit(" ", 1)
                cname = cname.strip()
                conf = float(cstr)
            except (ValueError, IndexError):
                continue
            raw_count[cname] += 1
            candidates.append((class_id, cx, cy, w, h, conf, cname))

        # Gate 1: area
        area_ok = []
        for c in candidates:
            _, _, _, w, h, _, _ = c
            area = w * h
            if area < MIN_AREA_FRAC or area > MAX_AREA_FRAC:
                continue
            area_ok.append(c)

        # Gate 2: aspect
        aspect_ok = []
        for c in area_ok:
            _, _, _, w, h, _, _ = c
            if w <= 0 or h <= 0:
                continue
            ratio = max(w / h, h / w)
            if ratio > MAX_ASPECT:
                continue
            aspect_ok.append(c)

        # Gate 5: confidence floors per class
        conf_ok = []
        for c in aspect_ok:
            class_id, cx, cy, w, h, conf, cname = c
            if conf < CONF_FLOORS.get(cname, 0.35):
                continue
            conf_ok.append(c)

        # Gate 3+4: cross-class NMS, then within-class NMS
        # Sort by confidence desc, keep best, suppress overlaps
        conf_ok.sort(key=lambda c: -c[5])
        kept = []
        for c in conf_ok:
            cls, cx, cy, w, h, conf, cname = c
            keep = True
            for k in kept:
                k_cls, k_cx, k_cy, k_w, k_h, _, _ = k
                iou = _iou((cx, cy, w, h), (k_cx, k_cy, k_w, k_h))
                if cls == k_cls:
                    if iou > WITHIN_CLASS_IOU_THR:
                        keep = False
                        break
                else:
                    if iou > CROSS_CLASS_IOU_THR:
                        keep = False
                        break
            if keep:
                kept.append(c)
                kept_count[cname] += 1

        # Write filtered label file (.txt, no comments — Ultralytics format)
        lines = [f"{c[0]} {c[1]:.6f} {c[2]:.6f} {c[3]:.6f} {c[4]:.6f}" for c in kept]
        (dst / (sheet + ".txt")).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

        per_image_summary.append({"image": sheet, "raw": len(candidates), "kept": len(kept)})

    summary = {
        "raw_class_totals": dict(raw_count),
        "kept_class_totals": dict(kept_count),
        "per_image": per_image_summary,
        "filters": {
            "MAX_AREA_FRAC": MAX_AREA_FRAC,
            "MIN_AREA_FRAC": MIN_AREA_FRAC,
            "MAX_ASPECT": MAX_ASPECT,
            "CROSS_CLASS_IOU_THR": CROSS_CLASS_IOU_THR,
            "WITHIN_CLASS_IOU_THR": WITHIN_CLASS_IOU_THR,
            "CONF_FLOORS": CONF_FLOORS,
        },
    }
    (Path(f"/data/{dst_subdir}") / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    dataset_volume.commit()

    print()
    print("[filter] class totals (raw -> kept):")
    for name in CLASS_NAMES:
        r = raw_count.get(name, 0)
        k = kept_count.get(name, 0)
        pct = (k / r * 100) if r else 0
        print(f"  {name:30s} {r:>5d} -> {k:>5d}  ({pct:>5.1f}% kept)")

    return summary


@app.local_entrypoint()
def main():
    print("Filter GroundingDINO labels for YOLO training")
    print("=" * 60)
    result = filter_labels.remote()
    print()
    print("DONE — total kept:", sum(result["kept_class_totals"].values()))
