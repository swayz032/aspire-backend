"""Aspire Drew SEE — Wave 11a-v3: pre-train YOLOv11n on Roboflow public datasets.

Strategy (after FloorPlanCAD HuggingFace turned out to be unlabeled):
  1. Download 2-3 public YOLO-format blueprint datasets from Roboflow Universe.
  2. Combine into one YOLO dataset, remapping each source's class IDs into
     Aspire's 14-class taxonomy via best-effort name matching.
  3. Fine-tune YOLOv11n on the combined set + our 80 real fixtures (when
     they become available).
  4. Save weights to Modal Volume `aspire-blueprint-models/aspire_v1.pt`.

Datasets (free, public on Roboflow Universe):
  - KoBidding/cobidding-plumbing-model       (plumbing symbols, pre-trained)
  - university-y9nbi/floor-plans-500         (Jan 2026 floor plans, YOLO format)
  - <add more as we find them>

Auth: API key passed via Modal secret `roboflow-api-key` (not hardcoded).
Cost: ~$2-3 on A100-40GB (1-2 hours training).
"""

from __future__ import annotations

import modal

APP_NAME = "aspire-drew-yolo-pretrain-rf"
app = modal.App(APP_NAME)

dataset_volume = modal.Volume.from_name("aspire-blueprint-dataset", create_if_missing=True)
models_volume = modal.Volume.from_name("aspire-blueprint-models", create_if_missing=True)

# Modal secret carrying the Roboflow API key. Created out-of-band via
# `modal secret create roboflow-api-key ROBOFLOW_API_KEY=<key>` so the key
# never lives in code or commit history. Modal injects it as an env var
# inside containers that declare `secrets=[...]`.
ROBOFLOW_SECRET = modal.Secret.from_name("roboflow-api-key")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0", "libxcb1", "wget", "unzip")
    .pip_install(
        "ultralytics>=8.3.0,<9.0",
        "Pillow>=10.0.0",
        "pyyaml>=6.0",
        "numpy>=1.24.0",
        "roboflow>=1.1.50",
        "tqdm>=4.66.0",
    )
    .run_commands("python -c 'from ultralytics import YOLO; YOLO(\"yolo11n.pt\")'")
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

# Generic substring → Aspire class mapping. Applied to each source dataset's
# class names. Anything that doesn't match is dropped from the merged dataset.
NAME_RULES: list[tuple[str, str]] = [
    # plumbing
    ("toilet", "plumbing_fixture_wc"),
    ("wc", "plumbing_fixture_wc"),
    ("water closet", "plumbing_fixture_wc"),
    ("water_closet", "plumbing_fixture_wc"),
    ("lavatory", "plumbing_fixture_lav"),
    ("lav ", "plumbing_fixture_lav"),
    ("sink", "plumbing_fixture_lav"),
    ("basin", "plumbing_fixture_lav"),
    ("urinal", "plumbing_fixture_wc"),
    ("floor drain", "plumbing_floor_drain"),
    ("floor_drain", "plumbing_floor_drain"),
    ("riser", "plumbing_riser"),
    # electrical
    ("outlet", "electrical_outlet"),
    ("socket", "electrical_outlet"),
    ("receptacle", "electrical_outlet"),
    ("duplex", "electrical_outlet"),
    ("gfci", "electrical_outlet"),
    ("panel", "electrical_panel"),
    ("circuit breaker", "electrical_panel"),
    ("breaker", "electrical_panel"),
    ("distribution", "electrical_panel"),
    ("mdp", "electrical_panel"),
    ("switch", "electrical_switch"),
    ("dimmer", "electrical_switch"),
    ("light fixture", "light_fixture"),
    ("light_fixture", "light_fixture"),
    ("ceiling light", "light_fixture"),
    ("ceiling_light", "light_fixture"),
    ("can light", "light_fixture"),
    ("can_light", "light_fixture"),
    ("recessed", "light_fixture"),
    ("troffer", "light_fixture"),
    ("pendant", "light_fixture"),
    ("light pole", "light_pole"),
    ("light_pole", "light_pole"),
    ("pole light", "light_pole"),
    ("pole_light", "light_pole"),
    ("conduit", "electrical_conduit_run"),
    ("cable tray", "electrical_conduit_run"),
    # architectural
    ("door", "door"),
    ("window", "window"),
    # structural
    ("column", "structural_column"),
    ("pillar", "structural_column"),
    ("post", "structural_column"),
    ("rebar", "rebar_callout"),
    ("reinforcement", "rebar_callout"),
]


def _map_name(src_name: str) -> str | None:
    """Map a source dataset's class name → Aspire class name. None if unmapped."""
    n = (src_name or "").lower().strip().replace("-", " ").replace("_", " ")
    if not n:
        return None
    # Exact match first
    if n in ASPIRE_NAME_TO_ID:
        return n
    # Substring rules (longest match wins to handle "light fixture" before "light")
    matches = [(rule, target) for rule, target in NAME_RULES if rule in n]
    if matches:
        matches.sort(key=lambda kv: -len(kv[0]))
        return matches[0][1]
    return None


@app.function(
    image=image,
    timeout=60 * 60,
    volumes={"/data": dataset_volume},
    secrets=[ROBOFLOW_SECRET],
)
def download_roboflow() -> dict:
    """Download Roboflow datasets and merge into one YOLO training set.

    Sources are hardcoded inside the function (Modal CLI doesn't support
    list-of-tuple parameters). Edit `_SOURCES` to add datasets.

    Output:
        /data/pretrain/roboflow/{images,labels}/{train,val}/...
        /data/pretrain/roboflow/data.yaml
        /data/pretrain/roboflow/summary.json
    """
    import json
    import os
    import shutil
    from collections import Counter
    from pathlib import Path

    import yaml as _y
    from roboflow import Roboflow

    # Public Roboflow Universe blueprint datasets — edit here to add/remove.
    _SOURCES = [
        ("kobidding", "cobidding-plumbing-model", 1),
        ("university-y9nbi", "floor-plans-500", 1),
    ]
    sources = _SOURCES

    api_key = os.environ.get("ROBOFLOW_API_KEY") or os.environ.get("ROBOFLOW_KEY")
    if not api_key:
        return {"error": "ROBOFLOW_API_KEY env var missing — pass via Modal secret"}

    rf = Roboflow(api_key=api_key)

    out_root = Path("/data/pretrain/roboflow")
    images_train = out_root / "images" / "train"
    images_val = out_root / "images" / "val"
    labels_train = out_root / "labels" / "train"
    labels_val = out_root / "labels" / "val"
    for d in (images_train, images_val, labels_train, labels_val):
        d.mkdir(parents=True, exist_ok=True)

    source_stats: list[dict] = []
    class_counter: Counter = Counter()
    file_idx = 0
    val_every = 5  # 20% val split

    for workspace, project_name, version in sources:
        prefix = f"{workspace}_{project_name}".replace("-", "_").replace("/", "_")
        print(f"[rf] downloading {workspace}/{project_name} v{version}...")
        try:
            project = rf.workspace(workspace).project(project_name)
            dataset = project.version(version).download(
                model_format="yolov11",
                location=f"/tmp/rf_{prefix}",
                overwrite=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            source_stats.append({"source": f"{workspace}/{project_name}", "error": str(exc)})
            continue

        ds_dir = Path(dataset.location)
        # Parse the source data.yaml to learn the source class list
        src_yaml_path = ds_dir / "data.yaml"
        if not src_yaml_path.exists():
            src_yaml_path = ds_dir / "data.yml"
        if not src_yaml_path.exists():
            print(f"  ERROR: data.yaml missing in {ds_dir}")
            source_stats.append({"source": f"{workspace}/{project_name}", "error": "data.yaml missing"})
            continue
        src_yaml = _y.safe_load(src_yaml_path.read_text(encoding="utf-8"))
        src_names = src_yaml.get("names") or []
        print(f"  source classes ({len(src_names)}): {src_names[:8]}{'...' if len(src_names) > 8 else ''}")

        # Build source-id → Aspire-id mapping
        src_to_aspire: dict[int, int] = {}
        for src_id, src_name in enumerate(src_names):
            mapped = _map_name(src_name)
            if mapped and mapped in ASPIRE_NAME_TO_ID:
                src_to_aspire[src_id] = ASPIRE_NAME_TO_ID[mapped]
        print(f"  mapped {len(src_to_aspire)} / {len(src_names)} source classes to Aspire")

        # Walk each split (train, valid, test) and convert
        kept = 0
        skipped = 0
        for split_name in ("train", "valid", "test"):
            split_dir = ds_dir / split_name
            if not split_dir.exists():
                continue
            imgs_src = split_dir / "images"
            lbls_src = split_dir / "labels"
            if not imgs_src.exists() or not lbls_src.exists():
                continue
            for label_file in lbls_src.glob("*.txt"):
                # Find matching image (any extension)
                stem = label_file.stem
                img_candidate = None
                for ext in (".jpg", ".jpeg", ".png", ".JPG", ".PNG"):
                    p = imgs_src / f"{stem}{ext}"
                    if p.exists():
                        img_candidate = p
                        break
                if img_candidate is None:
                    skipped += 1
                    continue

                # Remap class IDs
                lines_in = label_file.read_text(encoding="utf-8").splitlines()
                lines_out: list[str] = []
                for line in lines_in:
                    parts = line.strip().split()
                    if len(parts) < 5:
                        continue
                    try:
                        src_id = int(parts[0])
                        if src_id not in src_to_aspire:
                            continue
                        aspire_id = src_to_aspire[src_id]
                        cx, cy, w, h = parts[1], parts[2], parts[3], parts[4]
                        lines_out.append(f"{aspire_id} {cx} {cy} {w} {h}")
                        class_counter[ASPIRE_CLASSES[aspire_id]] += 1
                    except (ValueError, IndexError):
                        continue

                if not lines_out:
                    skipped += 1
                    continue

                # Write to merged dataset
                dst_dir_imgs = images_val if (file_idx % val_every == 0) else images_train
                dst_dir_lbls = labels_val if (file_idx % val_every == 0) else labels_train
                out_stem = f"{prefix}_{file_idx:06d}"
                shutil.copy2(img_candidate, dst_dir_imgs / f"{out_stem}{img_candidate.suffix.lower()}")
                (dst_dir_lbls / f"{out_stem}.txt").write_text("\n".join(lines_out) + "\n", encoding="utf-8")
                kept += 1
                file_idx += 1

        print(f"  kept {kept}, skipped {skipped}")
        source_stats.append({
            "source": f"{workspace}/{project_name}",
            "src_classes": src_names,
            "mapped_classes": list(src_to_aspire.keys()),
            "kept_images": kept,
            "skipped_images": skipped,
        })

    # Write merged data.yaml (path resolved from the data.yaml file's directory)
    data_yaml = {
        "path": str(out_root),
        "train": "images/train",
        "val": "images/val",
        "nc": len(ASPIRE_CLASSES),
        "names": ASPIRE_CLASSES,
    }
    (out_root / "data.yaml").write_text(_y.safe_dump(data_yaml), encoding="utf-8")

    summary = {
        "sources": source_stats,
        "class_counts": dict(class_counter),
        "total_images_kept": file_idx,
        "data_yaml": str(out_root / "data.yaml"),
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    dataset_volume.commit()

    print()
    print(f"[rf] DONE — merged {file_idx} images, {sum(class_counter.values())} labels")
    print("[rf] class counts:")
    for name in ASPIRE_CLASSES:
        print(f"  {name:30s} {class_counter.get(name, 0):>6d}")

    return summary


@app.function(
    image=image,
    gpu="A100-40GB",
    timeout=3 * 60 * 60,
    volumes={"/data": dataset_volume, "/models": models_volume},
)
def train_pretrain(
    data_yaml: str = "/data/pretrain/roboflow/data.yaml",
    epochs: int = 80,
    batch_size: int = 16,
    img_size: int = 1024,
    patience: int = 15,
    out_name: str = "aspire_v1",
) -> dict:
    import os
    from pathlib import Path

    os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/ultralytics")
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
    from ultralytics import YOLO

    if not Path(data_yaml).exists():
        return {"error": f"data.yaml missing at {data_yaml}"}

    print(f"[train] yolo11n base + fine-tune on {data_yaml}")
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
        fliplr=0.0,
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
    print(f"[train] DONE — mAP@0.50: {best_map:.4f}, mAP@0.50-0.95: {best_map_5095:.4f}")
    return {
        "weights": str(dest),
        "map_50": best_map,
        "map_50_95": best_map_5095,
    }


@app.local_entrypoint()
def main():
    print("=" * 60)
    print("Wave 11a-v3 — Roboflow Universe pre-train")
    print("=" * 60)
    print()
    print("Step 1: download + merge")
    dl = download_roboflow.remote()
    if "error" in dl:
        print("Download failed:", dl)
        return
    if dl.get("total_images_kept", 0) < 50:
        print(f"Too few images kept ({dl.get('total_images_kept')}) — check class mapping")
        return
    print()
    print("Step 2: train")
    result = train_pretrain.remote()
    print()
    print("=" * 60)
    print("TRAINING DONE")
    print(result)
    print("=" * 60)
