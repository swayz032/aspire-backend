"""Wave 20.14.5J-V3-PRG-r9 — Raster Falsification.

Hypothesis: PRG depends on vector primitive continuity. Rasterization breaks:
  - line endpoint topology (closed-quad detection requires endpoint coincidence)
  - parallel-line clustering
  - microcell motif detection

Test: rasterize representative PDFs at 150dpi, embed as new raster-only PDFs,
run PRG against them. Measure drop in baseline_found rate.

EXPECTED OUTCOME: baseline_found collapses on raster PDFs.
SURPRISE OUTCOME: PRG works on raster somehow (validates architecture further).
EITHER WAY: this is the honest falsification.

No new model code. Just an adversarial pipeline test using existing PRG.
"""
import sys
import io
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))
from prg_v29 import process_pdf as prg_process_pdf  # noqa: E402

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
RASTER_DIR = Path(r"C:\Users\tonio\Projects\myapp\blueprints\raster_r9")
RASTER_DIR.mkdir(exist_ok=True)

# Representative subset (one per project class)
PDFS_TO_RASTERIZE = [
    "29187P-ES (3).pdf",                    # Mangonia commercial structural
    "GAVNN_ADDENDUM_1.pdf",                 # Gulfstream commercial multi-discipline
    "Southern_Farmhouse_JayOsborne.pdf",    # Residential architectural
    "American_Farmhouse_201225.pdf",        # Residential architectural (different firm)
]


def rasterize_pdf(src_path: Path, dst_path: Path, dpi: int = 150) -> int:
    """Render each page of src_path to image at dpi, embed in new PDF at dst_path.
    Returns number of pages rasterized."""
    src = fitz.open(str(src_path))
    dst = fitz.open()
    zoom = dpi / 72.0  # PDF native dpi is 72
    mat = fitz.Matrix(zoom, zoom)
    n = 0
    try:
        for page_idx in range(src.page_count):
            page = src[page_idx]
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("png")
            # Create new page with same dimensions, embed image
            new_page = dst.new_page(width=page.rect.width, height=page.rect.height)
            rect = new_page.rect
            new_page.insert_image(rect, stream=img_bytes)
            n += 1
    finally:
        dst.save(str(dst_path), garbage=4, deflate=True)
        dst.close()
        src.close()
    return n


def main():
    print("=== Wave 20.14.5J-V3-PRG-r9 RASTER FALSIFICATION ===\n", flush=True)
    print("Hypothesis: PRG depends on vector continuity. Rasterization will break it.\n", flush=True)

    raster_paths = []
    for fname in PDFS_TO_RASTERIZE:
        src = BLUEPRINTS / fname
        if not src.exists():
            print(f"  SKIP {fname} (not found)", flush=True)
            continue
        dst = RASTER_DIR / f"raster150_{src.stem}.pdf"
        if not dst.exists():
            print(f"  rasterizing {fname} at 150dpi...", flush=True)
            n = rasterize_pdf(src, dst, dpi=150)
            print(f"    -> {dst.name}  ({n} pages)", flush=True)
        else:
            print(f"  using cached {dst.name}", flush=True)
        raster_paths.append(dst)

    print(f"\n--- running PRG on {len(raster_paths)} rasterized PDFs ---\n", flush=True)
    all_results = []
    for dst in raster_paths:
        print(f"=== {dst.name} ===", flush=True)
        r = prg_process_pdf(dst)
        all_results.append(r)

    print("\n=== RASTER FALSIFICATION VERDICT ===", flush=True)
    total_pages = 0
    base_found = 0
    follows = 0
    ablation_survives = 0
    for r in all_results:
        for pr in r.get("page_results", []):
            if "error" in pr:
                continue
            total_pages += 1
            if pr.get("baseline_found"):
                base_found += 1
                if pr.get("ablation_survives"):
                    ablation_survives += 1
                if pr.get("follows_content"):
                    follows += 1

    print(f"total raster pages tested:    {total_pages}")
    print(f"baseline_found:               {base_found}  ({base_found/max(total_pages,1)*100:.1f}%)")
    print(f"ablation_survives:            {ablation_survives}  ({ablation_survives/max(base_found,1)*100:.1f}% of found)")
    print(f"follows_content:              {follows}  ({follows/max(base_found,1)*100:.1f}% of found)")
    print(f"\nv29 r8 vector baseline_found: 84.8% (128/151)")
    print(f"v29 r9 raster baseline_found: {base_found/max(total_pages,1)*100:.1f}% ({base_found}/{total_pages})")
    drop = (0.848 - base_found/max(total_pages,1)) * 100
    print(f"DROP: {drop:+.1f}pt")
    if base_found / max(total_pages, 1) < 0.20:
        print(f"\nVERDICT: PRG is VECTOR-DEPENDENT. Rasterization collapsed baseline detection.")
        print(f"         Hybrid raster+vector pipeline required before production.")
    elif base_found / max(total_pages, 1) > 0.70:
        print(f"\nVERDICT: SURPRISE — PRG survives rasterization. Architecture more robust than expected.")
    else:
        print(f"\nVERDICT: PARTIAL — PRG degrades on raster but doesn't fully collapse.")


if __name__ == "__main__":
    main()
