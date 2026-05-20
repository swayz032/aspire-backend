"""Aspire Drew SEE — Modal GroundingDINO auto-labeler for Wave 11a.

Zero-shot object detection: feed PNG + text prompts like "electrical outlet",
"door swing", "plumbing fixture" — GroundingDINO returns bounding boxes
for each detected instance.

Workflow:
  1) `python scripts/render_fixtures_for_labeling.py` produced 80 PNGs
  2) Upload PNGs to Modal Volume `aspire-blueprint-dataset/raw_images/`:
       modal volume put aspire-blueprint-dataset ./scripts/labeling_workspace/raw_images/ /raw_images/
  3) `modal run blueprint_yolo_autolabel.py` runs detection on all PNGs
  4) Outputs YOLO-format labels + visual overlays at /auto_labels/ in the volume
  5) I download a sample, visually verify, then proceed to training

Industry pattern (April 2026 Traffic Synth / Roboflow AutoDistill):
  GroundingDINO → YOLO labels, then YOLO training on those labels.
  Acts as a "knowledge distillation" from a large zero-shot model into a
  small fast specialized model.

Cost: T4 GPU, ~2-3 sec per sheet × 80 sheets = ~4 min. Modal cost: ~$0.30.
"""

from __future__ import annotations

import modal

APP_NAME = "aspire-drew-yolo-autolabel"

app = modal.App(APP_NAME)

dataset_volume = modal.Volume.from_name(
    "aspire-blueprint-dataset",
    create_if_missing=True,
)

# GroundingDINO image. HuggingFace IDEA-Research/grounding-dino-tiny is the
# small variant — 172M params, fits on T4, runs at ~2-3s per 1500px image.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0", "libxcb1", "wget")
    .pip_install(
        "transformers>=4.45.0",
        "torch>=2.4.0",
        "Pillow>=10.0.0",
        "pyyaml>=6.0",
        "numpy>=1.24.0",
    )
    .run_commands(
        # Pre-cache the model into the image so warm starts skip the download.
        "python -c \"from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection; "
        "AutoProcessor.from_pretrained('IDEA-Research/grounding-dino-tiny'); "
        "AutoModelForZeroShotObjectDetection.from_pretrained('IDEA-Research/grounding-dino-tiny')\""
    )
)

# The 14 Aspire classes mapped to GroundingDINO text prompts. Prompts use
# natural language — model is zero-shot, so phrasing matters.
# Class ID order MUST match aspire_class_taxonomy.yaml (locked).
CLASS_PROMPTS = [
    # id 0
    ("electrical_outlet", "electrical outlet symbol . duplex outlet . wall outlet . receptacle"),
    # id 1
    ("electrical_panel", "electrical panel . circuit breaker panel . main panel . sub panel"),
    # id 2
    ("electrical_switch", "light switch symbol . wall switch . dimmer switch . three way switch"),
    # id 3
    ("light_fixture", "ceiling light fixture . recessed light . pendant light . troffer light"),
    # id 4
    ("light_pole", "outdoor light pole . pole mounted fixture . parking lot pole light"),
    # id 5
    ("electrical_conduit_run", "electrical conduit . wiring conduit . cable tray"),
    # id 6
    ("plumbing_fixture_wc", "toilet . water closet . WC"),
    # id 7
    ("plumbing_fixture_lav", "sink . lavatory . wash basin . hand sink"),
    # id 8
    ("plumbing_floor_drain", "floor drain . FD . drain symbol on floor"),
    # id 9
    ("plumbing_riser", "vertical pipe riser . plumbing riser symbol"),
    # id 10
    ("door", "door symbol . door swing . door opening . hinged door"),
    # id 11
    ("window", "window symbol on wall . casement window . sliding window"),
    # id 12
    ("structural_column", "structural column . concrete column . steel column . column on plan"),
    # id 13
    ("rebar_callout", "rebar callout . rebar size annotation . reinforcement note"),
]

# Per-class confidence thresholds. GroundingDINO scores tend to be lower than
# YOLO conf scores. We err on the side of recall here — bad labels are
# easier to filter than missing ones.
CLASS_THRESHOLDS = {
    "electrical_outlet": 0.30,
    "electrical_panel": 0.30,
    "electrical_switch": 0.30,
    "light_fixture": 0.30,
    "light_pole": 0.30,
    "electrical_conduit_run": 0.25,  # thin lines
    "plumbing_fixture_wc": 0.30,
    "plumbing_fixture_lav": 0.30,
    "plumbing_floor_drain": 0.30,
    "plumbing_riser": 0.25,
    "door": 0.30,
    "window": 0.30,
    "structural_column": 0.30,
    "rebar_callout": 0.20,  # tiny text annotations
}


@app.function(
    image=image,
    gpu="T4",
    timeout=20 * 60,
    volumes={"/data": dataset_volume},
)
def autolabel_all(
    images_subdir: str = "raw_images",
    out_subdir: str = "auto_labels_v1",
    max_per_class_per_image: int = 200,
) -> dict:
    """Auto-label every PNG in /data/<images_subdir>/ using GroundingDINO.

    Writes YOLO-format .txt labels to /data/<out_subdir>/labels/ and a
    summary.json with per-class detection counts.
    """
    import json
    from pathlib import Path

    import torch
    from PIL import Image
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

    print(f"[autolabel] loading GroundingDINO...")
    processor = AutoProcessor.from_pretrained("IDEA-Research/grounding-dino-tiny")
    model = AutoModelForZeroShotObjectDetection.from_pretrained(
        "IDEA-Research/grounding-dino-tiny"
    ).to("cuda")
    model.eval()

    images_dir = Path(f"/data/{images_subdir}")
    out_dir = Path(f"/data/{out_subdir}")
    labels_dir = out_dir / "labels"
    labels_dir.mkdir(parents=True, exist_ok=True)

    pngs = sorted(images_dir.glob("*.png"))
    print(f"[autolabel] found {len(pngs)} PNGs in {images_dir}")

    class_totals = {name: 0 for name, _ in CLASS_PROMPTS}
    summary: list[dict] = []

    for png_idx, png in enumerate(pngs):
        try:
            img = Image.open(png).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{png_idx + 1}/{len(pngs)}] skip {png.name}: {exc}")
            continue

        w, h = img.size

        # Per-class detection (GroundingDINO handles multi-prompt poorly with
        # very different scales, so we batch one class at a time for quality).
        yolo_lines: list[str] = []
        per_class_counts: dict[str, int] = {}

        for class_id, (class_name, prompt) in enumerate(CLASS_PROMPTS):
            inputs = processor(
                images=img,
                text=prompt + ".",  # GroundingDINO expects "." -terminated phrases
                return_tensors="pt",
            ).to("cuda")
            with torch.no_grad():
                outputs = model(**inputs)
            threshold = CLASS_THRESHOLDS.get(class_name, 0.30)
            results = processor.post_process_grounded_object_detection(
                outputs,
                inputs.input_ids,
                threshold=threshold,
                text_threshold=threshold,
                target_sizes=[img.size[::-1]],  # (h, w)
            )[0]

            boxes = results["boxes"].cpu().numpy()
            scores = results["scores"].cpu().numpy()

            count = 0
            for (x1, y1, x2, y2), score in zip(boxes, scores):
                if count >= max_per_class_per_image:
                    break
                # Skip tiny boxes (likely noise) or huge boxes (likely whole-image)
                bw, bh = float(x2 - x1), float(y2 - y1)
                if bw < 6 or bh < 6:
                    continue
                if bw > w * 0.85 and bh > h * 0.85:
                    continue
                # YOLO format: <class_id> <x_center> <y_center> <width> <height>
                # all normalized 0-1 by image dimensions.
                cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
                nw, nh = bw / w, bh / h
                yolo_lines.append(
                    f"{class_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}  # {class_name} {score:.3f}"
                )
                count += 1
                class_totals[class_name] += 1
            per_class_counts[class_name] = count

        # Write YOLO label .txt (one per image, same stem)
        label_path = labels_dir / (png.stem + ".txt")
        # Strip per-line comments for the real label file (Ultralytics ignores
        # them but cleaner to have pure data). Keep a `.commented.txt` for review.
        clean_lines = [line.split("  #")[0] for line in yolo_lines]
        label_path.write_text("\n".join(clean_lines) + ("\n" if clean_lines else ""), encoding="utf-8")
        (labels_dir / (png.stem + ".commented.txt")).write_text(
            "\n".join(yolo_lines) + ("\n" if yolo_lines else ""),
            encoding="utf-8",
        )

        summary.append({
            "image": png.name,
            "total_labels": len(yolo_lines),
            "per_class": per_class_counts,
        })
        print(f"  [{png_idx + 1}/{len(pngs)}] {png.name} -> {len(yolo_lines)} labels")

    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps({
            "class_totals": class_totals,
            "per_image": summary,
            "thresholds": CLASS_THRESHOLDS,
        }, indent=2),
        encoding="utf-8",
    )

    dataset_volume.commit()

    print()
    print("[autolabel] DONE — class_totals:")
    for name, total in sorted(class_totals.items(), key=lambda kv: -kv[1]):
        print(f"  {name:30s} {total:>5d}")
    print(f"[autolabel] summary written to {summary_path}")

    return {
        "class_totals": class_totals,
        "image_count": len(pngs),
        "labels_dir": str(labels_dir),
        "summary_path": str(summary_path),
    }


@app.local_entrypoint()
def main():
    """CLI: run auto-labeling end-to-end."""
    print("Aspire Drew GroundingDINO auto-label v1")
    print("=" * 60)
    result = autolabel_all.remote()
    print()
    print("Class totals:")
    for name, count in sorted(result["class_totals"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:30s} {count:>5d}")
    print()
    print(f"Image count: {result['image_count']}")
    print(f"Labels dir: {result['labels_dir']}")
    print()
    print("Next steps:")
    print("  1) Download a sample to spot-check:")
    print("       modal volume get aspire-blueprint-dataset auto_labels_v1/summary.json -")
    print("  2) Build train/val data.yaml + split images 80/20")
    print("  3) `modal run orchestrator/modal/blueprint_yolo_train.py`")
