# Wave 11a — YOLOv11n Fine-Tune on Real Aspire Fixtures

**Goal:** Replace COCO-trained YOLO11n weights (which detect `person/car/dog`) with Aspire-fine-tuned weights that detect 14 construction-symbol classes. This is the SEE-stage moat.

**Industry benchmark (SkeySpot 2026, arxiv 2508.10449):** YOLOv8 fine-tune on electrical symbols hits **82.5% mAP@0.50**. We target **>=75% mAP** across all 14 classes.

---

## 1. The 14-class taxonomy (LOCKED)

See `orchestrator/src/aspire_orchestrator/config/pack_policies/drew/aspire_class_taxonomy.yaml` for the full spec. Summary:

| Class ID | Name | Discipline |
|---|---|---|
| 0 | `electrical_outlet` | E |
| 1 | `electrical_panel` | E |
| 2 | `electrical_switch` | E |
| 3 | `light_fixture` | E |
| 4 | `light_pole` | LP |
| 5 | `electrical_conduit_run` | E |
| 6 | `plumbing_fixture_wc` | P |
| 7 | `plumbing_fixture_lav` | P |
| 8 | `plumbing_floor_drain` | P |
| 9 | `plumbing_riser` | P |
| 10 | `door` | A |
| 11 | `window` | A |
| 12 | `structural_column` | S |
| 13 | `rebar_callout` | S |

Class IDs are **positional and locked** — never renumber.

---

## 2. Labeling target

- **Per class: 200-500 instances minimum** (more for `electrical_outlet` and `door` which are common; can be lower for `light_pole`)
- **Source: our 11 real fixtures** at `orchestrator/tests/fixtures/blueprints/`:
  - `electrical_e1.pdf` to `electrical_e4.pdf` (E classes)
  - `plumbing_p1.pdf` (P classes)
  - `light_pole_lp1_r1.pdf`, `light_pole_revised.pdf` (LP class)
  - `concrete_mangonia_park.pdf`, `eng_rev1_signed_master.pdf` (S classes, doors, columns)
  - `eng_c2_2_gsm.pdf` (civil/site — mostly skipped except site lighting/utilities)
  - `gavnn_addendum_1.pdf` (mixed disciplines)

**Total estimated labeling time:** 8-16 hours for one person. Half-day with two labelers in parallel.

---

## 3. Labeling tool (recommended: Roboflow)

Roboflow free tier covers 1000 images for personal projects.

**Setup:**

1. Go to https://roboflow.com and create a new project:
   - Name: `aspire-drew-blueprint-symbols-v1`
   - Type: **Object Detection**
   - License: Private
2. Add the 14 classes above in **the exact same order**.
3. Upload the rendered PDFs as PNGs (200 DPI per Aspire SEE convention). The `orchestrator/src/aspire_orchestrator/services/blueprint/pdf_splitter.py` already produces these PNGs and stores them in the `blueprint-sheet-images` Supabase bucket — download from there or re-render locally with `pdf_splitter.split_pdf_to_sheets(...)`.
4. Label each symbol with a tight bounding box. Use Roboflow's auto-label assist (it's surprisingly good once you label ~50 examples — set confidence threshold to 0.6).
5. **Augmentations to apply** (in Roboflow's Generate step):
   - Rotate: ±5°
   - Brightness: ±15%
   - Saturation: ±25%
   - Noise: up to 2% pixels
   - **No flips** for blueprints (text gets mirrored)
6. Train/val split: **80/20 random**.
7. Export as **YOLOv8 PyTorch format** (works identically for YOLOv11).

**Alternative free tool:** CVAT (https://app.cvat.ai). Slower auto-label, no augmentation built-in.

---

## 4. Upload labeled dataset to Modal Volume

```bash
# From your local machine, after Roboflow export:
unzip aspire-drew-blueprint-symbols-v1.yolov8.zip -d ./dataset

# Upload to Modal Volume
modal volume put aspire-blueprint-dataset ./dataset/ /
```

Sanity check via:
```bash
modal run orchestrator/modal/blueprint_yolo_train.py::list_dataset
```

Expected output:
```
{'file_count': N, 'first_50': [{'path': 'data.yaml', ...}, ...]}
```

---

## 5. Train

```bash
modal run orchestrator/modal/blueprint_yolo_train.py
```

Cost: ~$2-3 on A100-40GB. Time: 60-90 min for 100 epochs on ~1000 labeled images.

Output:
```
[train] DONE — best_map@0.50: 0.7847
[train] best weights copied to /models/aspire_v1.pt
```

---

## 6. Ship trained weights to production

Edit `orchestrator/modal/blueprint_yolo_app.py`:

```python
# Add models volume to the function decorator
models_volume = modal.Volume.from_name("aspire-blueprint-models")

@app.function(
    image=image,
    gpu="T4",
    max_containers=10,
    min_containers=2,
    scaledown_window=300,
    timeout=60,
    volumes={"/models": models_volume},  # NEW
)
@modal.fastapi_endpoint(method="POST")
async def detect_symbols(request: Request) -> dict:
    ...
    if "model" not in _model_cache:
        # Wave 11a: load fine-tuned Aspire weights
        _model_cache["model"] = YOLO("/models/aspire_v1.pt")  # was "yolo11n.pt"
    model = _model_cache["model"]
    ...
```

Also update `orchestrator/src/aspire_orchestrator/services/blueprint/symbol_detector.py`:

The `class_map` translation (`coco_to_aspire`) becomes obsolete — Modal now returns Aspire class names directly. Keep the symbol_class_map.yaml fallback for the LOCAL CPU path (which still uses generic YOLO11n).

After redeploy:
```bash
modal deploy orchestrator/modal/blueprint_yolo_app.py
```

Then smoke test:
```bash
SMOKE_FIXTURE=orchestrator/tests/fixtures/blueprints/electrical_e1.pdf \
  python smoke_test_drew_v2.py
```

**Expected:** `blueprint_symbols` count goes from 0-4 to 50-200+ per multi-sheet electrical fixture.

---

## 7. Ship criteria

From `aspire_class_taxonomy.yaml::ship_thresholds`:

- overall mAP@0.50 >= 0.75
- electrical_outlet recall >= 0.80 (most common class)
- per-class mAP@0.50 >= 0.55 (no sparse class is broken)
- inference latency p95 <= 600ms on T4

If thresholds aren't met:
- Label more (most common reason)
- Increase `epochs` to 150
- Try YOLOv11s (10MB, slightly slower, more accurate) instead of yolo11n

---

## 8. Versioning

- v1: This wave — generic-to-Aspire weights swap. Goal: prove the pipeline works end-to-end.
- v2: Add 6 more sparse classes (HVAC, fire suppression, civil-site, signage, equipment, accessibility).
- v3: Customer-specific retraining (e.g., a contractor who only does residential remodels).

Bump `VERSION` in `blueprint_yolo_train.py` for each retrain. Old `aspire_v{N-1}.pt` stays on the volume as instant-rollback.

---

## 9. Cost summary

| Step | Cost | One-time |
|---|---|---|
| Labeling (8-16h × $25/hr) | $200-400 | Yes |
| Modal A100 training | $2-3 | Per training run |
| Modal T4 inference (existing) | ~$5-15/mo | Ongoing |
| **Total v1 ship** | **~$220 + $5-15/mo** | |

---

## 10. Open questions / decisions

- **Q: Use SAM (Segment Anything) to auto-label?** A: Not for v1 — SAM produces masks, we need bboxes. Manual is faster for 14 well-defined classes.
- **Q: Synthetic data via Stable Diffusion?** A: Not for v1 — we have 11 real fixtures, that's enough seed data. Synthetic is the v2 backup if labeling gets stale.
- **Q: Why YOLOv11n not YOLOv11s/m?** A: nano is 2.6MB → fast Modal warm-pool warmup, sub-200ms inference on T4. Sufficient for 14 well-separated classes per SkeySpot benchmark.
