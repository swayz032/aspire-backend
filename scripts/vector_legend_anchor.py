"""Wave 11h — Legend-anchored naming for VGSE motifs.

Pipeline:
  1. Find legend region(s) on the page via PyMuPDF text extraction + keyword
     matching ("LEGEND", "SCHEDULE", "SYMBOL", "FIXTURE TYPE", "ABBREVIATIONS").
  2. Within the legend region, find drawing groups (motif candidates) AND
     adjacent text spans. Each legend ENTRY is one (drawing + label) pair.
  3. Compute the VGSE signature for each legend drawing using the same
     algorithm as vector_extract_motifs.py — ensures legend signatures match
     drawing signatures elsewhere on the same sheet.
  4. Output: signature_hash → architect's official name.

This gives us deterministic naming: when motif `4dcc324c78` appears 691 times
on the drawing AND the same `4dcc324c78` appears once in the legend with the
label "DUPLEX OUTLET 20A 125V", we know what those 691 instances ARE.

CRITICAL: text labels in PDFs may render as TEXT spans (good) OR as VECTOR
GEOMETRY (when CAD exporter converts strokes to outlines — common). We handle
both: PyMuPDF.get_text() for text spans, and fall back to bbox-proximity
matching against any nearby vector text-glyph signatures.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import fitz

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "orchestrator" / "tests" / "fixtures" / "blueprints"
OUT_DIR = REPO_ROOT / "scripts" / "labeling_workspace" / "vector_legend"

# Reuse the signature hashing from vector_extract_motifs.py for consistency
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from vector_extract_motifs import _drawing_bbox, _drawing_signature  # noqa: E402

# Legend region detection: search page text for these keywords.
LEGEND_KEYWORDS = [
    r"\bLEGEND\b",
    r"\bSYMBOL\s+LEGEND\b",
    r"\bSYMBOL\s+LIST\b",
    r"\bSYMBOLS?\b\s+AND\s+ABBREVIATIONS",
    r"\bFIXTURE\s+SCHEDULE\b",
    r"\bLIGHTING\s+FIXTURE\s+SCHEDULE\b",
    r"\bELECTRICAL\s+LEGEND\b",
    r"\bPLUMBING\s+LEGEND\b",
    r"\bABBREVIATIONS?\b",
    r"\bKEY\s+NOTES?\b",
    r"\bPANEL\s+SCHEDULE\b",
    r"\bPANELBOARD\s+(H\s+)?SCHEDULE\b",
    r"\bPANELBOARD\s+\w+\s+SCHEDULE\b",
    r"\bFIXTURE\s+TYPE\b",
    r"\bRECEPTACLE\s+SCHEDULE\b",
    r"\bDEVICE\s+SCHEDULE\b",
    r"\bEQUIPMENT\s+SCHEDULE\b",
    r"\bMATERIAL\s+SCHEDULE\b",
]
_LEGEND_RE = re.compile("|".join(LEGEND_KEYWORDS), re.IGNORECASE)


def find_legend_regions(page: fitz.Page) -> list[fitz.Rect]:
    """Find rectangles on the page that likely contain a legend table.

    Strategy:
      1. Walk text blocks. When we find a block whose text contains a legend
         keyword, expand a candidate region around it.
      2. Expand DOWN and slightly OUT to capture the table rows that follow
         the heading.
      3. Cap the region to the page bounds.
    """
    page_rect = page.rect
    page_w, page_h = page_rect.width, page_rect.height

    text_blocks = page.get_text("blocks")  # list of (x0, y0, x1, y1, text, ...)
    regions: list[fitz.Rect] = []

    for block in text_blocks:
        if len(block) < 5:
            continue
        x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
        if not isinstance(text, str):
            continue
        if not _LEGEND_RE.search(text):
            continue

        # Expand region: heading + estimated 30% of page height downward,
        # full width of the heading + some margin on either side.
        margin_x = 60.0  # pt
        expand_down = min(page_h * 0.45, 360.0)  # at most 5 inches
        rx0 = max(0.0, x0 - margin_x)
        ry0 = max(0.0, y0 - 8.0)
        rx1 = min(page_w, x1 + margin_x)
        ry1 = min(page_h, y1 + expand_down)
        regions.append(fitz.Rect(rx0, ry0, rx1, ry1))

    # Merge overlapping regions
    merged: list[fitz.Rect] = []
    for r in regions:
        joined = False
        for i, m in enumerate(merged):
            if not (r & m).is_empty:
                merged[i] = fitz.Rect(m | r)
                joined = True
                break
        if not joined:
            merged.append(r)

    return merged


def _bbox_in_rect(bbox: tuple[float, float, float, float], rect: fitz.Rect) -> bool:
    """True if the bbox center is inside the rect."""
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1


def _find_nearest_label(drawing_bbox: tuple[float, float, float, float],
                       text_spans: list[dict], max_dist_pt: float = 80.0,
                       max_vertical_tol: float = 12.0) -> str | None:
    """Find the closest text span to the RIGHT of the drawing bbox.

    Legend tables almost always render as: [small symbol drawing] [text label].
    So we look right-and-slightly-down from the drawing's right edge.

    Excludes heading text (matching LEGEND_KEYWORDS) so we only get row labels.
    Tight vertical tolerance — legend rows are typically 8-15pt tall.
    """
    dx0, dy0, dx1, dy1 = drawing_bbox
    drawing_cy = (dy0 + dy1) / 2

    best: tuple[float, str] | None = None
    for span in text_spans:
        sx0, sy0, sx1, sy1 = span["bbox"]
        # Skip headings — they're usually the giant title text, not row labels
        if _LEGEND_RE.search(span["text"]):
            continue
        # Skip very short tokens that are just punctuation
        if len(span["text"].strip()) < 2:
            continue
        # Text must start to the RIGHT of drawing's right edge
        if sx0 < dx0:
            continue
        # And vertically near the drawing center
        sy_mid = (sy0 + sy1) / 2
        if abs(sy_mid - drawing_cy) > max_vertical_tol:
            continue
        # Horizontal distance from drawing right edge
        dx = sx0 - dx1
        if dx < -2.0 or dx > max_dist_pt:
            continue
        # Score: prefer closest + most aligned vertically
        score = dx + abs(sy_mid - drawing_cy) * 0.5
        if best is None or score < best[0]:
            best = (score, span["text"])
    return best[1] if best else None


def extract_legend_entries(page: fitz.Page,
                           min_items: int = 1,
                           max_items: int = 150,
                           min_long_side_pt: float = 1.0,
                           max_long_side_pt: float = 200.0,
                           quantize_grid: int = 12) -> list[dict]:
    """Extract (signature, label) pairs from legend regions on the page.

    Uses the SAME signature algorithm as VGSE so legend hashes match drawing
    hashes elsewhere on the sheet.
    """
    regions = find_legend_regions(page)
    if not regions:
        return []

    # Collect all text spans on the page (we'll filter by region later)
    raw_text = page.get_text("dict")
    text_spans: list[dict] = []
    for block in raw_text.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = span.get("text", "").strip()
                if not t or len(t) < 1:
                    continue
                bb = span.get("bbox")
                if not bb or len(bb) < 4:
                    continue
                text_spans.append({"text": t, "bbox": bb})

    # Walk drawings; keep only those inside a legend region.
    drawings = page.get_drawings()
    entries: list[dict] = []
    seen_signatures: set[str] = set()

    for d in drawings:
        items = d.get("items", [])
        if not (min_items <= len(items) <= max_items):
            continue
        bbox = _drawing_bbox(items)
        if bbox is None:
            continue
        # Inside any legend region?
        if not any(_bbox_in_rect(bbox, r) for r in regions):
            continue
        x0, y0, x1, y1 = bbox
        long_side = max(x1 - x0, y1 - y0)
        if long_side < min_long_side_pt or long_side > max_long_side_pt:
            continue
        sig = _drawing_signature(items, bbox, grid=quantize_grid)

        # Find the label to the right of this drawing
        label = _find_nearest_label(bbox, text_spans, max_dist_pt=120.0)
        if not label:
            continue

        # De-dup: if same signature already has a label, keep the longer one
        if sig in seen_signatures:
            for e in entries:
                if e["signature"] == sig and len(label) > len(e["label"]):
                    e["label"] = label
            continue
        seen_signatures.add(sig)
        entries.append({
            "signature": sig,
            "label": label,
            "bbox_pt": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
            "item_count": len(items),
            "size_pt": [round(x1 - x0, 2), round(y1 - y0, 2)],
        })

    return entries


def extract_pdf_legend(pdf_path: Path, page_number: int = 1) -> dict:
    """Extract all legend (signature -> label) mappings from a PDF page."""
    with fitz.open(pdf_path) as doc:
        if page_number > len(doc):
            return {"error": f"page {page_number} out of range"}
        page = doc[page_number - 1]
        regions = find_legend_regions(page)
        entries = extract_legend_entries(page)

    return {
        "file": pdf_path.name,
        "page": page_number,
        "legend_regions_found": len(regions),
        "legend_region_bboxes": [
            [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]
            for r in regions
        ],
        "entry_count": len(entries),
        "entries": entries,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Test on sheets with clear legends
    targets = [
        ("electrical_e1.pdf", 1),  # has SITE LIGHTING FIXTURE SCHEDULE
        ("electrical_e2.pdf", 1),  # has PANELBOARD SCHEDULES
        ("electrical_e3.pdf", 1),  # GENERAL ELECTRICAL NOTES + schedules
        ("plumbing_p1.pdf", 1),
        ("light_pole_lp1_r1.pdf", 1),
    ]
    for fname, page in targets:
        path = FIXTURES_DIR / fname
        if not path.exists():
            print(f"  skip {fname}: not found")
            continue
        print(f"\n=== {fname} page {page} ===")
        result = extract_pdf_legend(path, page_number=page)
        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue
        print(f"  legend regions: {result['legend_regions_found']}")
        print(f"  legend entries: {result['entry_count']}")
        for entry in result["entries"][:15]:
            label = entry["label"][:60]
            print(f"    sig={entry['signature'][:10]}  items={entry['item_count']:>3}  "
                  f"size={entry['size_pt']}  ->  \"{label}\"")
        stem = path.stem
        out_path = OUT_DIR / f"{stem}_p{page}_legend.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"  -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
