"""Aspire Drew SEE — Modal A100 YOLOv11n fine-tune training job.

Companion to `blueprint_yolo_app.py` (the inference endpoint). This script
runs the actual training on a Modal A100 GPU using a dataset uploaded to
Modal Volume.

Workflow:
  1) Label ~200-500 instances/class on Roboflow or CVAT against the 14
     classes defined in aspire_class_taxonomy.yaml.
  2) Export YOLOv8 format (YOLOv11n uses the same data.yaml format).
  3) Upload dataset to Modal Volume `aspire-blueprint-dataset`.
  4) Deploy this script: `modal run orchestrator/modal/blueprint_yolo_train.py`
  5) On success, the trained weights are saved to Modal Volume
     `aspire-blueprint-models` at `/models/aspire_v{N}.pt`.
  6) Update `blueprint_yolo_app.py` to load the new weights and redeploy.

Hardware: A100-40GB. ~$2/hour. Expected training time for 100 epochs on
500-1000 labeled images: 60-90 minutes. Total cost: ~$2-3 per training run.

To re-run with new data, bump VERSION and re-execute.

Industry benchmark we're targeting (SkeySpot 2026): 82.5% mAP@0.50 on
electrical symbols with YOLOv8 fine-tune. We expect to land at 75-85%.
"""

from __future__ import annotations

import modal
from pathlib import Path

VERSION = 1  # bump on each retraining run
APP_NAME = "aspire-drew-yolo-train"

app = modal.App(APP_NAME)

# Modal volumes (persistent storage)
dataset_volume = modal.Volume.from_name(
    "aspire-blueprint-dataset",
    create_if_missing=True,
)
models_volume = modal.Volume.from_name(
    "aspire-blueprint-models",
    create_if_missing=True,
)

# Training image — includes ultralytics, pytorch, CUDA.
train_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0", "libxcb1")
    .pip_install(
        "ultralytics>=8.3.0,<9.0",
        "Pillow>=10.0.0",
        "pyyaml>=6.0",
        "wandb>=0.18.0",  # optional metrics tracking; set WANDB_DISABLED=true to skip
    )
    .run_commands(
        # Pre-fetch base weights into the image so training starts fast.
        "python -c 'from ultralytics import YOLO; YOLO(\"yolo11n.pt\")'"
    )
)


@app.function(
    image=train_image,
    gpu="A100-40GB",
    timeout=2 * 60 * 60,  # 2-hour hard ceiling
    volumes={
        "/data": dataset_volume,
        "/models": models_volume,
    },
)
def train(
    data_yaml_path: str = "/data/data.yaml",
    epochs: int = 100,
    batch_size: int = 16,
    img_size: int = 1280,
    patience: int = 20,
) -> dict:
    """Run YOLOv11n fine-tune on the uploaded dataset.

    Args:
        data_yaml_path: Path inside the dataset volume to the data.yaml file.
            Standard YOLO format with `train`, `val`, `nc`, `names` keys.
        epochs: Training epochs (default 100). Most runs converge by 60-80.
        batch_size: Batch size (default 16 — fits A100-40GB at 1280px).
        img_size: Input resolution (default 1280 — construction symbols need it).
        patience: Early stop if val mAP plateaus for this many epochs.

    Returns:
        dict with `best_map`, `best_weights_path`, `metrics`.
    """
    import os
    from ultralytics import YOLO

    os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/ultralytics")
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

    print(f"[train] starting Aspire YOLOv11n v{VERSION} fine-tune")
    print(f"[train] data: {data_yaml_path}, epochs: {epochs}, batch: {batch_size}, imgsz: {img_size}")

    if not Path(data_yaml_path).exists():
        raise FileNotFoundError(
            f"data.yaml not found at {data_yaml_path}. Upload your dataset to the "
            f"`aspire-blueprint-dataset` Modal Volume first."
        )

    # Load base YOLOv11n weights
    model = YOLO("yolo11n.pt")

    # Fine-tune
    results = model.train(
        data=data_yaml_path,
        epochs=epochs,
        batch=batch_size,
        imgsz=img_size,
        patience=patience,
        optimizer="AdamW",
        lr0=0.001,
        weight_decay=0.0005,
        # Augmentations — from aspire_class_taxonomy.yaml
        mosaic=1.0,
        mixup=0.15,
        copy_paste=0.3,
        hsv_h=0.015,
        hsv_s=0.7,
        hsv_v=0.4,
        degrees=5.0,
        translate=0.1,
        scale=0.5,
        fliplr=0.5,
        # Save settings
        save=True,
        save_period=10,
        project="/tmp/runs",
        name=f"aspire_v{VERSION}",
        exist_ok=True,
        verbose=True,
        device=0,
    )

    # Find best weights file (Ultralytics convention: runs/<project>/<name>/weights/best.pt)
    best_path = Path(f"/tmp/runs/aspire_v{VERSION}/weights/best.pt")
    if not best_path.exists():
        raise RuntimeError(f"best.pt not produced at {best_path} — training failed silently")

    # Copy to the models volume so the inference app can load it.
    dest_path = Path(f"/models/aspire_v{VERSION}.pt")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(best_path.read_bytes())
    models_volume.commit()

    # Surface val metrics
    best_map = float(getattr(results, "results_dict", {}).get("metrics/mAP50(B)", 0.0))

    print(f"[train] DONE — best_map@0.50: {best_map:.4f}")
    print(f"[train] best weights copied to {dest_path}")

    return {
        "version": VERSION,
        "best_map_50": best_map,
        "weights_path_modal_volume": str(dest_path),
        "weights_path_local_runs": str(best_path),
        "epochs_completed": epochs,
    }


@app.function(image=train_image, volumes={"/data": dataset_volume})
def list_dataset() -> dict:
    """Sanity-check what's in the dataset volume. Run this BEFORE training to
    confirm your upload landed correctly.
    """
    base = Path("/data")
    if not base.exists():
        return {"error": "Volume not mounted"}

    entries = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            entries.append({"path": str(p.relative_to(base)), "size_kb": p.stat().st_size // 1024})
            if len(entries) >= 50:
                break
    return {"file_count": len(entries), "first_50": entries}


@app.local_entrypoint()
def main():
    """CLI entry: validate dataset → train → print result.

    Usage:
      modal run orchestrator/modal/blueprint_yolo_train.py
    """
    print("=" * 60)
    print(f"Aspire Drew YOLO fine-tune v{VERSION}")
    print("=" * 60)
    print()
    print("Step 1: dataset sanity check")
    summary = list_dataset.remote()
    print(summary)
    if "error" in summary or summary.get("file_count", 0) < 5:
        print("Dataset volume is empty or missing. Upload your labeled YOLO dataset to:")
        print("  Modal Volume `aspire-blueprint-dataset`")
        print("Expected structure:")
        print("  data.yaml                       (YOLO data config)")
        print("  images/train/*.png")
        print("  images/val/*.png")
        print("  labels/train/*.txt              (YOLO bbox format)")
        print("  labels/val/*.txt")
        return

    print()
    print("Step 2: training")
    result = train.remote()
    print()
    print("=" * 60)
    print("Training complete")
    print(result)
    print("=" * 60)
    print()
    print("Next steps:")
    print(f"  1) Update blueprint_yolo_app.py to load /models/aspire_v{VERSION}.pt")
    print(f"     instead of yolo11n.pt")
    print(f"  2) Add `volumes={{'/models': models_volume}}` to the function decorator")
    print(f"  3) modal deploy orchestrator/modal/blueprint_yolo_app.py")
    print(f"  4) Smoke test against a real fixture and confirm symbols_count > 0")
