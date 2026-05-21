"""Aspire Drew SEE — Wave 11a-v2: pre-train YOLOv11n on public blueprint datasets.

Strategy (2026 research-backed):
  1. Download FloorPlanCAD from HuggingFace (10,000 floor plans, 30 classes,
     line-grained annotations). License: BSD-2.
  2. Convert panoptic annotations → YOLO bbox format.
  3. Map FloorPlanCAD classes → Aspire 14-class taxonomy (lossy but useful).
  4. Train YOLOv11n on combined data for ~80 epochs on A100-40GB.
  5. Save weights to Modal Volume `aspire-blueprint-models/aspire_v1.pt`.

This is the BOOTSTRAP — gets us to ~75-80% mAP on blueprint categories
(matches Kreo's starting point per their published claims).

Wave 11b (later): fine-tune this base on our 80 real fixtures + tenant
data as it comes in (the actual moat).

Cost: ~$3-5 on Modal A100-40GB ($2/hr × 1.5-2.5hr).
"""

from __future__ import annotations

import modal

APP_NAME = "aspire-drew-yolo-pretrain"
app = modal.App(APP_NAME)

dataset_volume = modal.Volume.from_name("aspire-blueprint-dataset", create_if_missing=True)
models_volume = modal.Volume.from_name("aspire-blueprint-models", create_if_missing=True)

# Training image
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0", "libxcb1", "wget", "unzip")
    .pip_install(
        "ultralytics>=8.3.0,<9.0",
        "Pillow>=10.0.0",
        "pyyaml>=6.0",
        "numpy>=1.24.0",
        "datasets>=3.0.0",       # HuggingFace datasets
        "huggingface_hub>=0.24.0",
        "tqdm>=4.66.0",
    )
    .run_commands(
        "python -c 'from ultralytics import YOLO; YOLO(\"yolo11n.pt\")'",
    )
)

# Aspire 14-class taxonomy (LOCKED — matches aspire_class_taxonomy.yaml)
ASPIRE_CLASSES = [
    "electrical_outlet",       # 0
    "electrical_panel",        # 1
    "electrical_switch",       # 2
    "light_fixture",           # 3
    "light_pole",              # 4
    "electrical_conduit_run",  # 5
    "plumbing_fixture_wc",     # 6
    "plumbing_fixture_lav",    # 7
    "plumbing_floor_drain",    # 8
    "plumbing_riser",          # 9
    "door",                    # 10
    "window",                  # 11
    "structural_column",       # 12
    "rebar_callout",           # 13
]
ASPIRE_NAME_TO_ID = {n: i for i, n in enumerate(ASPIRE_CLASSES)}

# FloorPlanCAD class → Aspire class mapping (best-effort).
# FloorPlanCAD has 30 categories covering: walls, doors, windows, furniture,
# plumbing, electrical, etc. We map only what aligns with our 14 Aspire classes.
# Unmapped FloorPlanCAD classes are dropped (they're not in our taxonomy).
FPCAD_TO_ASPIRE = {
    # Doors / windows
    "door": "door",
    "Door": "door",
    "window": "window",
    "Window": "window",
    "single door": "door",
    "double door": "door",
    "sliding door": "door",
    # Plumbing
    "toilet": "plumbing_fixture_wc",
    "wc": "plumbing_fixture_wc",
    "water closet": "plumbing_fixture_wc",
    "sink": "plumbing_fixture_lav",
    "lavatory": "plumbing_fixture_lav",
    "wash basin": "plumbing_fixture_lav",
    "floor drain": "plumbing_floor_drain",
    "fd": "plumbing_floor_drain",
    # Electrical
    "switch": "electrical_switch",
    "outlet": "electrical_outlet",
    "socket": "electrical_outlet",
    "receptacle": "electrical_outlet",
    "lamp": "light_fixture",
    "light": "light_fixture",
    "ceiling light": "light_fixture",
    "panel": "electrical_panel",
    "electrical panel": "electrical_panel",
    "distribution board": "electrical_panel",
    # Structural
    "column": "structural_column",
    "post": "structural_column",
    "pillar": "structural_column",
}


@app.function(
    image=image,
    timeout=90 * 60,  # 90 min — Wave 11a-v2 first try timed out at 30min on 44%
    volumes={"/data": dataset_volume},
)
def download_floorplancad(max_samples: int = 5000) -> dict:
    """Stream FloorPlanCAD from HuggingFace + write YOLO dataset under /data/pretrain/.

    Uses `streaming=True` to iterate samples one at a time instead of downloading
    all ~10GB upfront. Stops after `max_samples` successful conversions to bound
    cost. Skips samples without mappable Aspire labels.

    Returns counts by class so we can decide whether to proceed to train.
    """
    import json
    from pathlib import Path

    from datasets import load_dataset
    from PIL import Image
    from tqdm import tqdm

    out_dir = Path("/data/pretrain/floorplancad")
    images_dir = out_dir / "images" / "train"
    labels_dir = out_dir / "labels" / "train"
    val_images = out_dir / "images" / "val"
    val_labels = out_dir / "labels" / "val"
    for d in (images_dir, labels_dir, val_images, val_labels):
        d.mkdir(parents=True, exist_ok=True)

    print(f"[pretrain] streaming Voxel51/FloorPlanCAD (max_samples={max_samples})...")
    try:
        ds = load_dataset("Voxel51/FloorPlanCAD", split="train", streaming=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[pretrain] streaming load failed: {type(exc).__name__}: {exc}")
        return {"error": str(exc), "fallback_needed": True}

    print(f"[pretrain] streaming initialized — iterating samples")
    # Peek the first sample to learn schema
    first = next(iter(ds))
    print(f"[pretrain] sample keys: {list(first.keys())}")
    # Re-init iterator since we consumed one
    ds = load_dataset("Voxel51/FloorPlanCAD", split="train", streaming=True)

    class_counts: dict[str, int] = {n: 0 for n in ASPIRE_CLASSES}
    written = 0
    val_split_every = 5  # 20% val

    iterator = tqdm(ds, desc="convert", total=max_samples)
    for idx, row in enumerate(iterator):
        if written >= max_samples:
            iterator.write(f"  reached max_samples={max_samples}, stopping")
            break
        try:
            img = row.get("image") or row.get("img")
            if img is None:
                continue
            if not isinstance(img, Image.Image):
                continue
            w, h = img.size

            # FloorPlanCAD bbox format: each row should have "objects" with
            # {"category": str, "bbox": [x, y, w, h]} or similar.
            objects = row.get("objects") or row.get("annotations") or []
            if isinstance(objects, dict):
                # HF datasets sometimes flatten as parallel arrays
                cats = objects.get("category") or objects.get("categories") or []
                bboxes = objects.get("bbox") or objects.get("bboxes") or []
                if not cats or not bboxes:
                    continue
                objects = [{"category": c, "bbox": b} for c, b in zip(cats, bboxes)]

            yolo_lines = []
            for obj in objects:
                cat = str(obj.get("category", "")).strip().lower()
                aspire_name = FPCAD_TO_ASPIRE.get(cat) or FPCAD_TO_ASPIRE.get(cat.title())
                if not aspire_name:
                    continue
                class_id = ASPIRE_NAME_TO_ID[aspire_name]
                bbox = obj.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                # bbox might be [x, y, w, h] (COCO) or [x1, y1, x2, y2]
                x1, y1, bw_or_x2, bh_or_y2 = bbox[:4]
                if bw_or_x2 < w and bh_or_y2 < h and (bw_or_x2 < x1 + bw_or_x2):
                    # Heuristic: looks like xywh
                    bw, bh = bw_or_x2, bh_or_y2
                else:
                    # x2y2 format
                    bw, bh = bw_or_x2 - x1, bh_or_y2 - y1
                if bw <= 0 or bh <= 0:
                    continue
                cx, cy = (x1 + bw / 2) / w, (y1 + bh / 2) / h
                nw, nh = bw / w, bh / h
                if not (0 < cx < 1 and 0 < cy < 1 and 0 < nw < 1 and 0 < nh < 1):
                    continue
                yolo_lines.append(f"{class_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")
                class_counts[aspire_name] += 1

            if not yolo_lines:
                continue  # skip images with no mappable labels

            is_val = (idx % val_split_every == 0)
            img_dir = val_images if is_val else images_dir
            lbl_dir = val_labels if is_val else labels_dir
            stem = f"fpcad_{idx:06d}"
            img.save(img_dir / f"{stem}.png", "PNG")
            (lbl_dir / f"{stem}.txt").write_text("\n".join(yolo_lines) + "\n", encoding="utf-8")
            written += 1
        except Exception as exc:  # noqa: BLE001
            iterator.write(f"  skip idx={idx}: {type(exc).__name__}")
            continue

    # Write YOLO data.yaml
    data_yaml = {
        "path": str(out_dir),
        "train": "images/train",
        "val": "images/val",
        "nc": len(ASPIRE_CLASSES),
        "names": ASPIRE_CLASSES,
    }
    import yaml as _y
    (out_dir / "data.yaml").write_text(_y.safe_dump(data_yaml), encoding="utf-8")

    summary = {
        "source": "Voxel51/FloorPlanCAD",
        "images_written": written,
        "class_counts": class_counts,
        "data_yaml": str(out_dir / "data.yaml"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    dataset_volume.commit()

    print()
    print(f"[pretrain] wrote {written} images")
    print("[pretrain] class counts:")
    for name in ASPIRE_CLASSES:
        print(f"  {name:30s} {class_counts[name]:>6d}")

    return summary


@app.function(
    image=image,
    gpu="A100-40GB",
    timeout=3 * 60 * 60,
    volumes={"/data": dataset_volume, "/models": models_volume},
)
def train_pretrain(
    data_yaml: str = "/data/pretrain/floorplancad/data.yaml",
    epochs: int = 80,
    batch_size: int = 16,
    img_size: int = 1024,  # smaller than 1280 to keep training fast
    patience: int = 15,
    out_name: str = "aspire_v1",
) -> dict:
    """Train YOLOv11n on the converted dataset. Saves best.pt to models volume."""
    import os
    from pathlib import Path

    os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/ultralytics")
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
    from ultralytics import YOLO

    if not Path(data_yaml).exists():
        return {"error": f"data.yaml missing at {data_yaml}"}

    print(f"[train] loading yolo11n.pt base + fine-tuning on {data_yaml}")
    model = YOLO("yolo11n.pt")
    results = model.train(
        data=data_yaml,
        epochs=epochs,
        batch=batch_size,
        imgsz=img_size,
        patience=patience,
        optimizer="AdamW",
        lr0=0.001,
        weight_decay=0.0005,
        mosaic=1.0,
        mixup=0.10,
        copy_paste=0.30,
        hsv_h=0.015,
        hsv_s=0.5,
        hsv_v=0.3,
        degrees=3.0,
        translate=0.08,
        scale=0.4,
        fliplr=0.0,   # never flip blueprints
        save=True,
        save_period=10,
        project="/tmp/runs",
        name=out_name,
        exist_ok=True,
        verbose=True,
        device=0,
    )

    best_src = Path(f"/tmp/runs/{out_name}/weights/best.pt")
    if not best_src.exists():
        return {"error": f"best.pt not produced at {best_src}"}

    dest = Path(f"/models/{out_name}.pt")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(best_src.read_bytes())
    models_volume.commit()

    best_map = float(getattr(results, "results_dict", {}).get("metrics/mAP50(B)", 0.0))
    best_map_5095 = float(getattr(results, "results_dict", {}).get("metrics/mAP50-95(B)", 0.0))

    print(f"[train] DONE — best_map@0.50: {best_map:.4f}, mAP@0.50-0.95: {best_map_5095:.4f}")
    print(f"[train] weights -> {dest}")
    return {
        "weights": str(dest),
        "map_50": best_map,
        "map_50_95": best_map_5095,
        "epochs_run": epochs,
    }


@app.local_entrypoint()
def main():
    print("=" * 60)
    print("Wave 11a-v2 — pre-train YOLOv11n on FloorPlanCAD")
    print("=" * 60)
    print()
    print("Step 1: download + convert dataset")
    dl = download_floorplancad.remote()
    print(dl)
    if "error" in dl:
        print("Dataset download failed — see logs above")
        return
    if dl.get("images_written", 0) < 100:
        print(f"Too few images written ({dl.get('images_written')}) — abort")
        return
    print()
    print("Step 2: train")
    result = train_pretrain.remote()
    print()
    print("=" * 60)
    print("TRAINING COMPLETE")
    print(result)
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1) Edit orchestrator/modal/blueprint_yolo_app.py to load /models/aspire_v1.pt")
    print("  2) modal deploy orchestrator/modal/blueprint_yolo_app.py")
    print("  3) python smoke_test_drew_v2.py  → expect symbols > 0")
