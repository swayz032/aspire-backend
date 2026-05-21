"""Aspire Drew SEE — LASE v0 Proof-of-Concept.

Legend-Adaptive Symbol Engine — Fraunhofer 2025 approach, productized.

Core hypothesis: every blueprint has its own recurring symbol patterns.
Without ANY training data, we can:
  1. Find candidate symbol regions via contour detection (free, fast)
  2. Embed each region with CLIP (general-purpose vision encoder)
  3. Cluster embeddings (DBSCAN) to discover distinct symbol types
  4. Each cluster = a "symbol class" specific to this blueprint
  5. Count instances per cluster → contractor knows quantities
  6. Optionally: match cluster representatives against the legend table
     (via OCR text proximity) to assign human-readable names

This sidesteps the "no public dataset for electrical/structural symbols"
problem ENTIRELY. We don't need labeled training data — the blueprint
itself defines what the symbols mean.

Cost: T4 GPU, ~5-15 sec per sheet (CLIP batch embed + DBSCAN), ~$0.001/sheet.

Output: JSON + annotated PNG showing cluster bboxes with cluster_id + count.
"""

from __future__ import annotations

import json

import modal

APP_NAME = "aspire-drew-lase-poc"
app = modal.App(APP_NAME)

dataset_volume = modal.Volume.from_name("aspire-blueprint-dataset", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0", "libxcb1")
    .pip_install(
        "transformers>=4.45.0",
        "torch>=2.4.0",
        "torchvision>=0.19.0",
        "Pillow>=10.0.0",
        "opencv-python-headless>=4.10.0",
        "numpy>=1.24.0",
        "scikit-learn>=1.5.0",
        "tqdm>=4.66.0",
        "easyocr>=1.7.0",  # text detection for masking out OCR noise from cluster candidates
        "openai>=1.50.0",  # VLM naming via gpt-4o-mini / gpt-5.4-mini
        "hdbscan>=0.8.40",  # adaptive density clustering — no eps tuning per GPT review
    )
    .run_commands(
        "python -c \"from transformers import AutoImageProcessor, AutoModel; "
        "AutoImageProcessor.from_pretrained('facebook/dinov2-small'); "
        "AutoModel.from_pretrained('facebook/dinov2-small')\"",
        # Pre-download EasyOCR models so cold-start is fast
        "python -c \"import easyocr; easyocr.Reader(['en'], gpu=False)\"",
    )
)


@app.function(
    image=image,
    gpu="T4",
    timeout=10 * 60,
    volumes={"/data": dataset_volume},
)
def discover_symbol_clusters(
    image_path_in_volume: str = "raw_images/electrical_e1_p1.png",
    out_subdir: str = "lase_poc_v0",
    min_symbol_size: int = 6,
    max_symbol_size: int = 120,
    pad: int = 4,
    dbscan_eps: float = 0.07,
    dbscan_min_samples: int = 6,
    min_density: float = 0.10,
    max_density: float = 0.60,
    text_mask_dilate: int = 15,
    min_stroke_width_px: int = 7,
    use_hdbscan: bool = True,
    min_cluster_size: int = 6,
) -> dict:
    """Discover recurring symbol clusters in a blueprint via CLIP + DBSCAN.

    Pipeline:
      1. Load PNG from Modal volume
      2. Adaptive threshold to binary (handles varied line weights)
      3. Connected components (8-connectivity)
      4. Filter components by size (min_symbol_size to max_symbol_size)
      5. Crop each + pad + resize to 224x224
      6. Batch CLIP image embed (224-d normalized vectors)
      7. DBSCAN clustering with cosine distance
      8. Annotate sheet: each cluster gets a unique color + box
      9. Save annotated PNG + summary JSON

    Returns clusters summary.
    """
    import json
    from collections import Counter
    from pathlib import Path

    import cv2
    import numpy as np
    import torch
    from PIL import Image, ImageDraw, ImageFont
    from sklearn.cluster import DBSCAN
    from transformers import AutoImageProcessor, AutoModel

    print(f"[lase] loading {image_path_in_volume}")
    img_path = Path("/data") / image_path_in_volume
    if not img_path.exists():
        return {"error": f"image not found: {img_path}"}

    pil = Image.open(img_path).convert("RGB")
    W, H = pil.size
    print(f"[lase] image {W}x{H}")

    # WAVE 11g — TEXT MASKING via EasyOCR. Detect text bboxes and zero them
    # out in the binary image BEFORE connected components. This is the
    # critical step: without it, text characters dominate the cluster pool
    # (3,876 char-cluster on E-1, 809 on gavnn-p25). With it, only true
    # symbols remain as candidates.
    print("[lase] running EasyOCR for text masking...")
    import easyocr
    reader = easyocr.Reader(["en"], gpu=True)
    ocr_results = reader.readtext(np.array(pil), detail=1)
    print(f"[lase] OCR found {len(ocr_results)} text regions")

    # Build a text-mask: 1 where text exists, 0 elsewhere. Dilate slightly so
    # bbox-clipped text doesn't survive at edges.
    text_mask = np.zeros((H, W), dtype=np.uint8)
    for (poly, _text, _conf) in ocr_results:
        pts = np.array(poly, dtype=np.int32).reshape(-1, 1, 2)
        cv2.fillPoly(text_mask, [pts], 255)
    if text_mask_dilate > 0:
        kernel_dil = cv2.getStructuringElement(cv2.MORPH_RECT, (text_mask_dilate, text_mask_dilate))
        text_mask = cv2.dilate(text_mask, kernel_dil, iterations=1)
    text_coverage_pct = 100 * (text_mask > 0).sum() / (H * W)
    print(f"[lase] text mask covers {text_coverage_pct:.1f}% of the sheet")

    # Adaptive threshold — blueprints have varied line weights
    cv = np.array(pil.convert("L"))
    bin_img = cv2.adaptiveThreshold(
        cv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 11, 2,
    )
    # Wave 11g (per GPT review): morphological close DISABLED — was merging
    # outlet circles with their wire homerun lines, killing fixture detection
    # on dense electrical sheets. Keep adaptive threshold as-is.

    # Subtract text mask — anywhere EasyOCR detected text becomes background.
    bin_img[text_mask > 0] = 0

    n_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        bin_img, connectivity=8
    )
    print(f"[lase] found {n_labels - 1} connected components (incl background)")

    # Filter by size + aspect + DENSITY — density gate removes text characters
    # (typically 0.50-0.70 fill ratio) while keeping line-drawing symbols
    # (typically 0.15-0.45 fill ratio).
    candidates: list[tuple[int, int, int, int]] = []  # (x, y, w, h)
    for i in range(1, n_labels):  # skip background (label 0)
        x, y, w, h, area = stats[i]
        long_side = max(w, h)
        if long_side < min_symbol_size or long_side > max_symbol_size:
            continue
        if min(w, h) == 0:
            continue
        aspect = max(w, h) / max(1, min(w, h))
        if aspect > 6:
            continue
        bbox_area = max(1, w * h)
        density = area / bbox_area  # filled pixel ratio
        if density < min_density or density > max_density:
            continue
        # Stroke-width filter: digits like "1", "7" are thin vertical strokes
        # with min dim ~4-6px on 7200×4800 sheet. Real symbols have stroke
        # width >= 7px or non-vertical aspect.
        if min(w, h) < min_stroke_width_px and aspect > 2.5:
            continue
        candidates.append((int(x), int(y), int(w), int(h)))

    print(f"[lase] {len(candidates)} candidates after size/aspect filter")
    if len(candidates) < 10:
        return {
            "error": "too_few_candidates",
            "candidates": len(candidates),
            "hint": "tune min_symbol_size/max_symbol_size for this DPI",
        }

    # Crop, pad, resize each candidate to 224×224 for CLIP
    crops_pil: list[Image.Image] = []
    for (x, y, w, h) in candidates:
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(W, x + w + pad)
        y1 = min(H, y + h + pad)
        crop = pil.crop((x0, y0, x1, y1))
        # Square-pad to preserve aspect ratio
        side = max(x1 - x0, y1 - y0)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        offset = ((side - (x1 - x0)) // 2, (side - (y1 - y0)) // 2)
        square.paste(crop, offset)
        square = square.resize((224, 224), Image.LANCZOS)
        crops_pil.append(square)

    # Load DINOv2-small — much better than CLIP at fine-grained line-drawing
    # differences. CLIP was trained on natural images and tends to give similar
    # embeddings to all small black-on-white symbols. DINOv2 self-supervised
    # training preserves fine geometric detail.
    print("[lase] loading DINOv2-small...")
    processor = AutoImageProcessor.from_pretrained("facebook/dinov2-small")
    model = AutoModel.from_pretrained("facebook/dinov2-small").to("cuda")
    model.eval()

    # Batch embed
    print(f"[lase] embedding {len(crops_pil)} candidates...")
    embeddings: list[np.ndarray] = []
    batch_size = 128
    with torch.no_grad():
        for i in range(0, len(crops_pil), batch_size):
            batch = crops_pil[i:i + batch_size]
            inputs = processor(images=batch, return_tensors="pt")
            pixel_values = inputs["pixel_values"].to("cuda")
            outputs = model(pixel_values=pixel_values)
            # DINOv2 returns last_hidden_state; use CLS token (index 0) as the
            # image-level embedding. Shape: [B, hidden_dim=384 for small].
            feats = outputs.last_hidden_state[:, 0, :]
            feats = feats / feats.norm(dim=-1, keepdim=True)
            embeddings.append(feats.cpu().numpy())
    embeddings = np.vstack(embeddings)
    print(f"[lase] embeddings shape: {embeddings.shape}")

    # Wave 11g (per GPT review): HDBSCAN replaces DBSCAN — no eps tuning, adaptive
    # density per cluster, robust across architect styles + DPI variations.
    if use_hdbscan:
        try:
            import hdbscan
            print(f"[lase] HDBSCAN clustering min_cluster_size={min_cluster_size}")
            clusterer = hdbscan.HDBSCAN(
                min_cluster_size=min_cluster_size,
                min_samples=max(2, min_cluster_size // 2),
                metric="euclidean",  # embeddings are L2-normalized; euclidean ≈ cosine on unit sphere
                cluster_selection_method="eom",  # excess of mass — better than leaf for varying densities
            )
            cluster_ids = clusterer.fit_predict(embeddings)
            cluster_persistence = getattr(clusterer, "cluster_persistence_", None)
        except ImportError:
            print("[lase] hdbscan not available, falling back to DBSCAN")
            use_hdbscan = False

    if not use_hdbscan:
        print(f"[lase] DBSCAN clustering eps={dbscan_eps} min_samples={dbscan_min_samples}")
        clusterer = DBSCAN(eps=dbscan_eps, min_samples=dbscan_min_samples, metric="cosine")
        cluster_ids = clusterer.fit_predict(embeddings)
        cluster_persistence = None

    counts = Counter(cluster_ids.tolist())
    n_clusters = sum(1 for k in counts if k != -1)
    n_noise = counts.get(-1, 0)
    print(f"[lase] {n_clusters} clusters + {n_noise} noise points")

    # Build cluster summary
    cluster_summary: list[dict] = []
    for cid in sorted(counts.keys()):
        if cid == -1:
            continue
        members = [i for i, c in enumerate(cluster_ids) if c == cid]
        bboxes = [candidates[i] for i in members]
        cluster_summary.append({
            "cluster_id": int(cid),
            "count": len(members),
            "bbox_examples": bboxes[:5],
            "all_bboxes": bboxes,
        })
    cluster_summary.sort(key=lambda c: -c["count"])

    # Annotate the sheet: top 10 clusters in colored boxes
    palette = [
        (255, 0, 0), (0, 200, 0), (0, 0, 255), (255, 165, 0),
        (200, 0, 200), (0, 200, 200), (180, 100, 0), (100, 180, 100),
        (100, 100, 200), (200, 100, 100),
    ]
    annot = pil.copy()
    draw = ImageDraw.Draw(annot)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
    for i, cluster in enumerate(cluster_summary[:10]):
        color = palette[i % len(palette)]
        cid = cluster["cluster_id"]
        cnt = cluster["count"]
        for (x, y, w, h) in cluster["all_bboxes"]:
            draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
        # Label the FIRST instance with the cluster summary
        first = cluster["all_bboxes"][0]
        x, y = first[0], first[1]
        draw.rectangle([x, y - 20, x + 80, y - 2], fill=color)
        draw.text((x + 2, y - 18), f"#{cid} x{cnt}", fill=(255, 255, 255), font=font)

    out_dir = Path("/data") / out_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(image_path_in_volume).stem
    annot_path = out_dir / f"{stem}_clusters.png"
    annot.save(annot_path, "PNG")

    # Save a montage of cluster representatives for visual review
    if cluster_summary:
        montage_w = 224 * min(10, len(cluster_summary))
        montage_h = 224 + 30
        montage = Image.new("RGB", (montage_w, montage_h), (240, 240, 240))
        mdraw = ImageDraw.Draw(montage)
        for i, cluster in enumerate(cluster_summary[:10]):
            cid = cluster["cluster_id"]
            cnt = cluster["count"]
            # Use the embedding-index of the first member as a representative
            first_idx = next(j for j, c in enumerate(cluster_ids) if c == cid)
            rep_img = crops_pil[first_idx]
            montage.paste(rep_img, (i * 224, 30))
            mdraw.text((i * 224 + 4, 6), f"#{cid}  n={cnt}", fill=(0, 0, 0), font=font)
        montage.save(out_dir / f"{stem}_montage.png", "PNG")

    summary = {
        "image": image_path_in_volume,
        "image_size": [W, H],
        "candidate_components": len(candidates),
        "cluster_count": n_clusters,
        "noise_count": n_noise,
        "top_clusters": [
            {"cluster_id": c["cluster_id"], "count": c["count"]}
            for c in cluster_summary[:20]
        ],
        "params": {
            "min_symbol_size": min_symbol_size,
            "max_symbol_size": max_symbol_size,
            "dbscan_eps": dbscan_eps,
            "dbscan_min_samples": dbscan_min_samples,
        },
        "outputs": {
            "annotated_png": str(annot_path.relative_to("/data")),
            "montage_png": str((out_dir / f"{stem}_montage.png").relative_to("/data")),
        },
    }
    (out_dir / f"{stem}_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    dataset_volume.commit()

    print()
    print("[lase] DONE — top 10 clusters by count:")
    for c in cluster_summary[:10]:
        print(f"  cluster #{c['cluster_id']:>3}  n={c['count']:>4}")
    print()
    print(f"[lase] annotated: {annot_path}")
    return summary


@app.function(
    image=image,
    timeout=10 * 60,
    volumes={"/data": dataset_volume},
    secrets=[modal.Secret.from_name("openai-api-key")],
)
def name_clusters_with_vlm(
    image_path_in_volume: str,
    out_subdir: str = "lase_poc_v0",
    model_name: str = "gpt-5.4-mini",
) -> dict:
    """Send each cluster's representative crop to a VLM and get a name.

    Discovery (above) is class-agnostic — it just finds repeating shapes.
    Naming uses GPT-4o-mini-vision to convert the visual cluster into a
    construction-symbol label. Costs ~$0.001-0.003 per cluster.

    The VLM prompt is grounded with construction context so it returns names
    in our domain (e.g., "electrical outlet" not "small black circle").
    """
    import base64
    import io
    import json as _json
    import os as _os
    from pathlib import Path

    from openai import OpenAI
    from PIL import Image

    stem = Path(image_path_in_volume).stem
    out_dir = Path("/data") / out_subdir
    summary_path = out_dir / f"{stem}_summary.json"
    if not summary_path.exists():
        return {"error": f"summary not found at {summary_path} — run discover_symbol_clusters first"}

    summary = _json.loads(summary_path.read_text(encoding="utf-8"))
    montage_path = out_dir / f"{stem}_montage.png"
    if not montage_path.exists():
        return {"error": f"montage not found at {montage_path}"}

    montage = Image.open(montage_path)
    M_W, M_H = montage.size
    # Each cluster panel is 224 wide. n=montage_width/224 panels.
    n_panels = M_W // 224
    print(f"[vlm] montage has {n_panels} cluster panels")

    client = OpenAI(api_key=_os.environ.get("OPENAI_API_KEY"))
    named: list[dict] = []

    for i in range(n_panels):
        panel = montage.crop((i * 224, 30, (i + 1) * 224, M_H))
        # Upscale 2x for clearer VLM perception
        panel = panel.resize((448, 448), Image.LANCZOS)
        buf = io.BytesIO()
        panel.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        cluster_meta = summary.get("top_clusters", [{}])[i] if i < len(summary.get("top_clusters", [])) else {}
        cid = cluster_meta.get("cluster_id", i)
        count = cluster_meta.get("count", 0)

        # Wave 11g (per GPT review): top-3 candidates with confidence + entropy
        # → first-class `unknown` state. Never force a label on ambiguous shapes.
        prompt = (
            "This is a symbol cropped from a commercial construction blueprint. "
            f"It appears {count} times on the sheet.\n\n"
            "Return your TOP 3 BEST GUESSES, each with confidence 0-1. "
            "Categories: electrical, plumbing, architectural, structural, "
            "mechanical, annotation, dimension, hatch, text, unknown.\n\n"
            "Be honest about uncertainty. If the shape is ambiguous or you "
            "cannot tell, return category=unknown with low confidence for top1. "
            "Annotation/dimension/text/hatch = NOT a fixture symbol.\n\n"
            "JSON ONLY in this shape:\n"
            '{"candidates":['
            '{"name":"<name>","category":"<cat>","confidence":<0-1>},'
            '{"name":"<name>","category":"<cat>","confidence":<0-1>},'
            '{"name":"<name>","category":"<cat>","confidence":<0-1>}'
            ']}'
        )

        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }],
                max_completion_tokens=300,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content
            parsed = _json.loads(raw)
            candidates = parsed.get("candidates") or []
        except Exception as exc:  # noqa: BLE001
            err_str = f"{type(exc).__name__}: {str(exc)[:300]}"
            candidates = []
            parsed = {"error": err_str}
            print(f"  VLM call exc: {err_str}")

        # Entropy-gated decision per GPT review.
        # If top-1 confidence < 0.50 OR entropy > 1.0 (uniform-ish), force unknown.
        import math
        top = candidates[0] if candidates else None
        probs = [max(1e-6, float(c.get("confidence", 0.0))) for c in candidates[:3]]
        if probs:
            total = sum(probs)
            norm = [p / total for p in probs]
            entropy = -sum(p * math.log(p) for p in norm) if total > 0 else 0.0
        else:
            entropy = 0.0

        top1_conf = float(top.get("confidence", 0.0)) if top else 0.0
        if top is None or top1_conf < 0.50 or entropy > 1.0:
            final_name = "unknown"
            final_category = "unknown"
            final_provenance = "vlm_low_confidence"
        else:
            final_name = top.get("name", "unknown")
            final_category = top.get("category", "unknown")
            final_provenance = "vlm_categorized"

        named.append({
            "cluster_id": cid,
            "count": count,
            "name": final_name,
            "category": final_category,
            "confidence": top1_conf,
            "entropy": round(entropy, 3),
            "top3_candidates": candidates[:3],
            "provenance": final_provenance,
            "error": parsed.get("error"),
        })
        print(f"  cluster #{cid:>3} n={count:>4}  ->  {final_name:35s} ({final_category}, conf={top1_conf:.2f}, H={entropy:.2f}, src={final_provenance})")

    out_path = out_dir / f"{stem}_named.json"
    out_path.write_text(_json.dumps(named, indent=2), encoding="utf-8")
    dataset_volume.commit()

    print(f"\n[vlm] DONE — saved to {out_path}")
    return {"clusters_named": named, "out_path": str(out_path)}


@app.local_entrypoint()
def main(image: str = "raw_images/electrical_e1_p1.png", do_vlm: bool = True):
    print("=" * 60)
    print("LASE v0 — Legend-Adaptive Symbol Engine PoC")
    print("=" * 60)
    print()
    result = discover_symbol_clusters.remote(image_path_in_volume=image)
    print()
    print("Discovery result:")
    print(json.dumps({k: v for k, v in result.items() if k != "all_bboxes"}, indent=2))
    if do_vlm and "outputs" in result:
        print()
        print("=" * 60)
        print("VLM NAMING (gpt-4o-mini-vision)")
        print("=" * 60)
        named = name_clusters_with_vlm.remote(image_path_in_volume=image)
        print()
        print("Named result:")
        print(json.dumps(named, indent=2))
    print()
    print("To visually inspect:")
    print(f"  modal volume get aspire-blueprint-dataset {result['outputs']['annotated_png']} -")
    print(f"  modal volume get aspire-blueprint-dataset {result['outputs']['montage_png']} -")
