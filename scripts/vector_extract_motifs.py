"""Wave 11g-v2 Stages B+C+D — extract vector primitives, build path signatures,
find repeated structural motifs in a blueprint PDF.

The breakthrough hypothesis: in a vector-native PDF, every CAD symbol instance
appears as a discrete "drawing" (path) in page.get_drawings(). Even when the
visual symbol is connected to a wire/wall, the symbol's own primitives form a
local cluster of paths with a consistent geometric signature (sequence of
line/curve/circle items + relative coordinates).

If we compute a translation-invariant signature for each drawing and hash it,
duplicate symbols MUST collide on the same hash. That gives us perfect
detection without any model — pure geometry.

Stages:
  B. Extract: page.get_drawings() → list of drawings, each with items[]
  C. Signature: for each drawing, compute a canonicalized signature
     (item type sequence + relative-position quantized hash)
  D. Mine: count instances per signature, return top-N motifs

Output: signature → (count, bbox examples, sample item list)
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import fitz  # pymupdf

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "orchestrator" / "tests" / "fixtures" / "blueprints"
OUT_DIR = REPO_ROOT / "scripts" / "labeling_workspace" / "vector_motifs"


def _drawing_bbox(items) -> tuple[float, float, float, float] | None:
    """Compute bounding box of a drawing's items."""
    xs, ys = [], []
    for item in items:
        # Items are tuples like ('l', p1, p2), ('c', p1, p2, p3, p4), ('re', rect), ('qu', quad)
        op = item[0]
        if op == "l":  # line
            for p in item[1:]:
                xs.append(p.x)
                ys.append(p.y)
        elif op == "c":  # cubic bezier
            for p in item[1:]:
                xs.append(p.x)
                ys.append(p.y)
        elif op == "re":  # rectangle
            r = item[1]
            xs.extend([r.x0, r.x1])
            ys.extend([r.y0, r.y1])
        elif op == "qu":  # quad
            q = item[1]
            xs.extend([q.ul.x, q.ur.x, q.ll.x, q.lr.x])
            ys.extend([q.ul.y, q.ur.y, q.ll.y, q.lr.y])
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _drawing_signature(items, bbox: tuple[float, float, float, float], grid: int = 12) -> str:
    """Translation-invariant, scale-invariant signature for a drawing.

    Normalize coordinates to [0, grid-1] within the bbox, quantize, and
    concatenate item types + quantized coords into a string, then SHA-1 hash.

    This makes any two drawings with the same geometric shape produce the same
    signature regardless of where they appear on the page.
    """
    x0, y0, x1, y1 = bbox
    w = max(1e-6, x1 - x0)
    h = max(1e-6, y1 - y0)

    def q(p) -> str:
        # Quantize point into the [0, grid-1] cell
        cx = int(((p.x - x0) / w) * (grid - 1))
        cy = int(((p.y - y0) / h) * (grid - 1))
        return f"{cx},{cy}"

    def qr(r) -> str:
        cx0 = int(((r.x0 - x0) / w) * (grid - 1))
        cy0 = int(((r.y0 - y0) / h) * (grid - 1))
        cx1 = int(((r.x1 - x0) / w) * (grid - 1))
        cy1 = int(((r.y1 - y0) / h) * (grid - 1))
        return f"{cx0},{cy0}-{cx1},{cy1}"

    sig_parts: list[str] = []
    for item in items:
        op = item[0]
        if op == "l":
            sig_parts.append(f"l:{q(item[1])}>{q(item[2])}")
        elif op == "c":
            sig_parts.append(f"c:{q(item[1])}>{q(item[2])}>{q(item[3])}>{q(item[4])}")
        elif op == "re":
            sig_parts.append(f"re:{qr(item[1])}")
        elif op == "qu":
            q1 = item[1]
            sig_parts.append(
                f"qu:{int(((q1.ul.x - x0) / w) * (grid - 1))},{int(((q1.ul.y - y0) / h) * (grid - 1))}-"
                f"{int(((q1.lr.x - x0) / w) * (grid - 1))},{int(((q1.lr.y - y0) / h) * (grid - 1))}"
            )
        else:
            sig_parts.append(op)
    # Aspect ratio bucket (4 buckets so square symbols don't collide with elongated)
    aspect = w / max(1e-6, h)
    if aspect < 0.5:
        ab = "tall"
    elif aspect < 1.5:
        ab = "square"
    elif aspect < 3.0:
        ab = "wide"
    else:
        ab = "veryWide"
    body = f"{ab}|" + "|".join(sig_parts)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def extract_motifs(
    pdf_path: Path,
    page_number: int = 1,
    min_items: int = 1,
    max_items: int = 80,
    min_long_side_pt: float = 2.0,
    max_long_side_pt: float = 80.0,
    quantize_grid: int = 12,
) -> dict:
    """Extract repeated motifs from a single page of a vector PDF."""
    with fitz.open(pdf_path) as doc:
        if page_number > len(doc):
            return {"error": f"page {page_number} out of range (doc has {len(doc)})"}
        page = doc[page_number - 1]
        drawings = page.get_drawings()
        page_w, page_h = page.rect.width, page.rect.height

        sig_to_instances: dict[str, list[dict]] = defaultdict(list)
        skipped_too_small = 0
        skipped_too_big = 0
        skipped_no_bbox = 0

        for idx, d in enumerate(drawings):
            items = d.get("items", [])
            if not (min_items <= len(items) <= max_items):
                continue
            bbox = _drawing_bbox(items)
            if bbox is None:
                skipped_no_bbox += 1
                continue
            x0, y0, x1, y1 = bbox
            w, h = x1 - x0, y1 - y0
            long_side = max(w, h)
            if long_side < min_long_side_pt:
                skipped_too_small += 1
                continue
            if long_side > max_long_side_pt:
                skipped_too_big += 1
                continue
            sig = _drawing_signature(items, bbox, grid=quantize_grid)
            sig_to_instances[sig].append({
                "drawing_index": idx,
                "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                "item_count": len(items),
                "size_pt": [round(w, 2), round(h, 2)],
            })

    # Rank signatures by count
    ranked = sorted(sig_to_instances.items(), key=lambda kv: -len(kv[1]))
    top_motifs = []
    for sig, instances in ranked[:50]:
        top_motifs.append({
            "signature": sig,
            "count": len(instances),
            "sample_size_pt": instances[0]["size_pt"] if instances else None,
            "sample_item_count": instances[0]["item_count"] if instances else None,
            "bbox_examples": [i["bbox"] for i in instances[:5]],
            "all_bboxes": [i["bbox"] for i in instances],
        })

    return {
        "file": pdf_path.name,
        "page": page_number,
        "page_size_pt": [round(page_w, 1), round(page_h, 1)],
        "total_drawings": len(drawings),
        "skipped_too_small": skipped_too_small,
        "skipped_too_big": skipped_too_big,
        "skipped_no_bbox": skipped_no_bbox,
        "unique_signatures": len(sig_to_instances),
        "top_motifs": top_motifs,
    }


def render_motif_overlay(pdf_path: Path, page_number: int, motifs: list[dict],
                        out_path: Path, top_k: int = 12, dpi: int = 150) -> None:
    """Render the page with the top-K motifs highlighted in distinct colors."""
    from PIL import Image, ImageDraw, ImageFont

    with fitz.open(pdf_path) as doc:
        page = doc[page_number - 1]
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
    except Exception:
        font = ImageFont.load_default()

    palette = [
        (255, 0, 0), (0, 200, 0), (0, 0, 255), (255, 140, 0),
        (200, 0, 200), (0, 200, 200), (180, 80, 0), (100, 180, 100),
        (100, 100, 200), (200, 100, 100), (60, 60, 60), (255, 215, 0),
    ]

    for i, motif in enumerate(motifs[:top_k]):
        color = palette[i % len(palette)]
        sig = motif["signature"][:8]
        cnt = motif["count"]
        for bbox in motif["all_bboxes"]:
            x0, y0, x1, y1 = bbox
            # Convert PDF points to image pixels
            x0p, y0p, x1p, y1p = x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom
            draw.rectangle([x0p, y0p, x1p, y1p], outline=color, width=2)
        # Label first instance
        if motif["all_bboxes"]:
            fx, fy, _, _ = motif["all_bboxes"][0]
            fxp, fyp = fx * zoom, fy * zoom
            label = f"#{i + 1}:{sig} x{cnt}"
            tw = len(label) * 7
            draw.rectangle([fxp, max(0, fyp - 18), fxp + tw, fyp - 2], fill=color)
            draw.text((fxp + 2, max(0, fyp - 18)), label, fill=(255, 255, 255), font=font)

    img.save(out_path, "PNG")


def _spatial_entropy(bboxes: list[list[float]], page_size: list[float], grid: int = 8) -> float:
    """Compute distribution entropy of motif instances across a grid over the page.

    Real fixtures distribute across plan area → high entropy.
    Text glyphs cluster in title block / schedules → low entropy.
    Range: [0, ~log2(grid*grid)] = [0, ~6 for grid=8].
    """
    import math
    if not bboxes:
        return 0.0
    page_w, page_h = page_size
    cells: dict[tuple[int, int], int] = {}
    for bbox in bboxes:
        x0, y0, x1, y1 = bbox
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        gx = min(grid - 1, max(0, int(cx / max(1e-6, page_w) * grid)))
        gy = min(grid - 1, max(0, int(cy / max(1e-6, page_h) * grid)))
        cells[(gx, gy)] = cells.get((gx, gy), 0) + 1
    total = sum(cells.values())
    if total == 0:
        return 0.0
    entropy = 0.0
    for c in cells.values():
        p = c / total
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def filter_real_symbols(top_motifs: list[dict], page_size: list[float],
                       min_items: int = 3, min_count: int = 5,
                       min_entropy: float = 1.5, max_size_pt: float = 40.0) -> list[dict]:
    """Wave 11g-v2: filter to REAL symbols (fixtures, not text/decoration).

    Filters applied:
      - min_items >= 3: skip single-line / dimension-tick noise
      - min_count >= 5: only meaningful repetition
      - spatial_entropy >= 1.5: distributed across plan, not clustered in text
      - max bbox long side <= 40pt: skip page-spanning elements
    """
    out = []
    for m in top_motifs:
        if (m.get("sample_item_count") or 0) < min_items:
            continue
        if m.get("count", 0) < min_count:
            continue
        size = m.get("sample_size_pt") or [0, 0]
        if max(size) > max_size_pt:
            continue
        ent = _spatial_entropy(m.get("all_bboxes", []), page_size)
        m["spatial_entropy"] = round(ent, 2)
        if ent < min_entropy:
            continue
        out.append(m)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Run on the densest electrical sheet first — most rigorous test
    targets = [
        ("electrical_e2.pdf", 1),
        ("electrical_e3.pdf", 1),
        ("plumbing_p1.pdf", 1),
        ("eng_rev1_signed_master.pdf", 1),
    ]
    for fname, page in targets:
        path = FIXTURES_DIR / fname
        if not path.exists():
            print(f"  skip {fname}: not found")
            continue
        print(f"\n=== {fname} page {page} ===")
        result = extract_motifs(path, page_number=page)
        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue
        print(f"  total drawings: {result['total_drawings']:,}")
        print(f"  unique signatures: {result['unique_signatures']:,}")
        print(f"  skipped (too small/big/no-bbox): "
              f"{result['skipped_too_small']:,} / {result['skipped_too_big']:,} / {result['skipped_no_bbox']:,}")
        # Filter to REAL symbols (>=3 items per drawing) — separates fixtures
        # from line-segment / dimension-tick noise.
        real_symbols = filter_real_symbols(
            result["top_motifs"],
            page_size=result["page_size_pt"],
            min_items=3,
            min_count=5,
            min_entropy=1.5,
            max_size_pt=40.0,
        )
        result["real_symbol_count"] = len(real_symbols)
        result["real_symbol_total_instances"] = sum(m["count"] for m in real_symbols)
        result["real_symbols"] = real_symbols[:30]

        print(f"  top 10 (ALL motifs by count):")
        for i, motif in enumerate(result["top_motifs"][:10]):
            print(f"    #{i + 1}  sig={motif['signature'][:10]}  "
                  f"count={motif['count']:>5}  "
                  f"items={motif['sample_item_count']:>3}  "
                  f"size={motif['sample_size_pt']}")
        print(f"  REAL SYMBOLS (items>=3, count>=5): {len(real_symbols)} types, "
              f"{result['real_symbol_total_instances']:,} total instances")
        for i, motif in enumerate(real_symbols[:15]):
            print(f"    #{i + 1}  sig={motif['signature'][:10]}  "
                  f"count={motif['count']:>5}  "
                  f"items={motif['sample_item_count']:>3}  "
                  f"size={motif['sample_size_pt']}  "
                  f"entropy={motif.get('spatial_entropy', 0):>4.2f}")

        # Save JSON + overlay (real symbols only — much cleaner viz)
        stem = path.stem
        (OUT_DIR / f"{stem}_p{page}_motifs.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        try:
            overlay_path = OUT_DIR / f"{stem}_p{page}_overlay_real.png"
            render_motif_overlay(path, page, real_symbols, overlay_path)
            print(f"  overlay (real only): {overlay_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"  overlay render failed: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
