"""
Blueprint fixture PII redaction script — Law #9 compliance.

Run once per Wave 2B to produce committed fixtures from originals.
Searches for engineer names, P.E. numbers, owner names, street addresses.
Redacts using PyMuPDF page.add_redact_annot() + page.apply_redactions().
Outputs REDACTION_REPORT.json alongside committed PDFs.

Usage:
    cd backend/orchestrator/tests/fixtures/blueprints
    python redact_fixtures.py

Requirements:
    pymupdf >= 1.24.0 (already in requirements.txt via parallel wave-2-ingest-classify)
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pymupdf  # type: ignore

# ---------------------------------------------------------------------------
# Source → committed filename map
# ---------------------------------------------------------------------------
FIXTURE_MAP: dict[str, str] = {
    # Source path : committed filename
    "gavnn_addendum_1": {
        "src": "C:/Users/tonio/Downloads/GAVNN_ ADDENDUM 1 (6) (2).pdf",
        "dst": "gavnn_addendum_1.pdf",
    },
    "eng_rev1_signed_master": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/21030 ENG_Rev1 240801_Signed.pdf",
        "dst": "eng_rev1_signed_master.pdf",
    },
    "eng_c2_2_gsm": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/21030 ENG-C2.2 GSM.pdf",
        "dst": "eng_c2_2_gsm.pdf",
    },
    "eng_c2_2_gsm_duplicate": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/21030 ENG-C2.2 GSM (1).pdf",
        "dst": "eng_c2_2_gsm_duplicate.pdf",
    },
    "electrical_e1": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/E-1 (1).pdf",
        "dst": "electrical_e1.pdf",
    },
    "electrical_e2": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/E-2 (1).pdf",
        "dst": "electrical_e2.pdf",
    },
    "electrical_e3": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/E-3 (1).pdf",
        "dst": "electrical_e3.pdf",
    },
    "electrical_e4": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/E-4 (1).pdf",
        "dst": "electrical_e4.pdf",
    },
    "plumbing_p1": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/P-1 (1).pdf",
        "dst": "plumbing_p1.pdf",
    },
    "light_pole_lp1_r1": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/2101LP-R1-LP1-1of1 (11) (7).pdf",
        "dst": "light_pole_lp1_r1.pdf",
    },
    "light_pole_revised": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/Revised light pole plan.pdf",
        "dst": "light_pole_revised.pdf",
    },
    "concrete_mangonia_park": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/Concrete blueprints Mangonia Park.pdf",
        "dst": "concrete_mangonia_park.pdf",
    },
    "precast_drainage": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/Precast Drainage.pdf",
        "dst": "precast_drainage.pdf",
    },
    "electrical_site_29187p_es": {
        "src": "C:/Users/tonio/Projects/myapp/blueprints/29187P-ES (3).pdf",
        "dst": "electrical_site_29187p_es.pdf",
    },
}

# ---------------------------------------------------------------------------
# Redaction patterns (case-insensitive text search targets)
# ---------------------------------------------------------------------------

# Keyword labels that precede PII — we redact the LABEL + VALUE together
PII_LABEL_PATTERNS: list[str] = [
    "Designed by",
    "Drawn by",
    "Engineer of Record",
    "Engineer:",
    "Eng:",
    "Sealed by",
    "Seal:",
    "P.E.#",
    "P.E. #",
    "P.E. No",
    "PE No",
    "License No",
    "License #",
    "Lic #",
    "Lic. No",
    "Checked by",
    "Owner:",
    "Owner of Record",
    "Client:",
    "Prepared by",
    "Prepared For",
]

# Standalone regex patterns for street addresses and zip codes in isolation
# We keep city+state but redact zip codes when they appear on standalone lines
ADDRESS_REGEX = re.compile(
    r"\b\d{1,6}\s+(?:(?:N|S|E|W|NE|NW|SE|SW|North|South|East|West)\s+)?"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*"
    r"\s+(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Way|Court|Ct|Lane|Ln|Place|Pl)\b",
    re.IGNORECASE,
)

# Also catch address patterns ending in numeric street names (e.g., "1319 53rd STREET")
ADDRESS_NUMERIC_STREET_REGEX = re.compile(
    r"\b\d{1,6}\s+\d+(?:st|nd|rd|th)\s+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Way|Blvd)\b",
    re.IGNORECASE,
)

# Zip code on its own (keep city/state, redact zip)
ZIP_REGEX = re.compile(r"\b\d{5}(?:-\d{4})?\b")

# P.E. number patterns — for REDACTION during processing (broad):
# "P.E. 1319", "PE 69188", "LICENSE NO. 69188", "License # 12345"
# Also catch spans containing "P.E." which signal engineer name spans
PE_NUMBER_REGEX = re.compile(
    r"(?:"
    r"P\.?E\.?\s*#?\s*\d{4,7}"
    r"|License\s+(?:No\.?|#|Number)\s*\d{5,10}"
    r"|\bP\.E\.\b"  # catch standalone P.E. designation (always in engineer name context)
    r")",
    re.IGNORECASE,
)

# Tighter P.E. pattern for POST-REDACTION VERIFICATION only.
# Must be followed by a digit or name (avoids false positives from "SCOPE", "TAPE", etc.)
# Also catches "License No. 69188" patterns that survived.
PE_VERIFY_REGEX = re.compile(
    r"(?:"
    r"\bP\.E\.\s*#?\s*\d{4,7}"          # P.E. followed by license number
    r"|License\s+(?:No\.?|#|Number)\s*\d{5,10}"  # License No. XXXXX
    r")",
    re.IGNORECASE,
)


def _redact_page(page: pymupdf.Page) -> dict[str, int]:
    """
    Find and redact all PII on a single page.
    Returns counts of each category found+redacted.
    """
    counts: dict[str, int] = {
        "label_patterns": 0,
        "street_addresses": 0,
        "zip_codes": 0,
        "pe_numbers": 0,
    }

    # 1. Label-based search (engineer/owner labels)
    for label in PII_LABEL_PATTERNS:
        rects = page.search_for(label, quads=False)
        for rect in rects:
            # Expand rect rightward to catch the value following the label
            # (typical title block has value on same line or slightly right)
            expanded = pymupdf.Rect(rect.x0, rect.y0 - 2, rect.x1 + 300, rect.y1 + 2)
            page.add_redact_annot(expanded, fill=(0, 0, 0))
            counts["label_patterns"] += 1

    # 2. Full-text extraction for regex-based redaction
    text_dict = page.get_text("dict")
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:  # 0 = text block
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"]
                bbox = span["bbox"]
                span_rect = pymupdf.Rect(bbox)

                # Street address (word street names)
                if ADDRESS_REGEX.search(text):
                    page.add_redact_annot(span_rect, fill=(0, 0, 0))
                    counts["street_addresses"] += 1
                # Street address (numeric street names like "1319 53rd STREET")
                elif ADDRESS_NUMERIC_STREET_REGEX.search(text):
                    page.add_redact_annot(span_rect, fill=(0, 0, 0))
                    counts["street_addresses"] += 1

                # Zip code (redact zip portion only — we keep city/state text)
                if ZIP_REGEX.search(text) and not ADDRESS_REGEX.search(text):
                    # Only redact if the span is likely a standalone address line
                    # (contains a zip but not already caught as full address)
                    for m in ZIP_REGEX.finditer(text):
                        # We don't have char-level rect; redact the whole span
                        page.add_redact_annot(span_rect, fill=(0, 0, 0))
                        counts["zip_codes"] += 1
                        break  # one redact per span is enough

                # P.E. number
                if PE_NUMBER_REGEX.search(text):
                    page.add_redact_annot(span_rect, fill=(0, 0, 0))
                    counts["pe_numbers"] += 1

    page.apply_redactions()
    return counts


def redact_pdf(src_path: str, dst_path: str, originals_dir: str) -> dict:
    """
    Copy src to originals dir, redact, write to dst_path.
    Returns report dict for this file.
    """
    src = Path(src_path)
    dst = Path(dst_path)
    orig_dst = Path(originals_dir) / src.name

    if not src.exists():
        return {
            "committed_filename": dst.name,
            "source_filename": src.name,
            "status": "SOURCE_NOT_FOUND",
            "error": str(src_path),
        }

    # Copy to originals (gitignored)
    shutil.copy2(src, orig_dst)

    size_before = src.stat().st_size

    doc = pymupdf.open(str(src))
    page_count = len(doc)
    total_counts: dict[str, int] = {
        "label_patterns": 0,
        "street_addresses": 0,
        "zip_codes": 0,
        "pe_numbers": 0,
    }

    for page in doc:
        page_counts = _redact_page(page)
        for k in total_counts:
            total_counts[k] += page_counts[k]

    doc.save(str(dst), garbage=4, deflate=True)
    doc.close()

    size_after = dst.stat().st_size

    return {
        "committed_filename": dst.name,
        "source_filename": src.name,
        "status": "OK",
        "page_count": page_count,
        "size_before_bytes": size_before,
        "size_after_bytes": size_after,
        "redactions_applied": total_counts,
        "total_redactions": sum(total_counts.values()),
    }


def verify_no_pii(pdf_path: str) -> dict[str, bool]:
    """
    Post-redaction sanity check: extract text and search for residual PII signals.
    Returns dict of check_name -> passed (True = no residual found).
    """
    doc = pymupdf.open(pdf_path)
    full_text = "\n".join(page.get_text() for page in doc)
    doc.close()

    checks: dict[str, bool] = {}

    # Check each label pattern is not in text
    for label in PII_LABEL_PATTERNS:
        key = f"no_label_{label.lower().replace(' ', '_').replace(':', '').replace('.', '').replace('#', 'num')}"
        checks[key] = label.lower() not in full_text.lower()

    # P.E. number pattern — use tighter verification regex to avoid false positives
    # from words ending in "PE" like "SCOPE", "TAPE", etc.
    checks["no_pe_number_pattern"] = not bool(PE_VERIFY_REGEX.search(full_text))

    return checks


def main() -> None:
    here = Path(__file__).parent
    originals_dir = here / "originals"
    originals_dir.mkdir(exist_ok=True)

    report: list[dict] = []
    all_ok = True

    for key, entry in FIXTURE_MAP.items():
        src = entry["src"]
        dst = str(here / entry["dst"])
        print(f"  Redacting: {entry['dst']} ...", end=" ", flush=True)

        result = redact_pdf(src, dst, str(originals_dir))
        report.append(result)

        if result["status"] == "OK":
            # Verification pass
            checks = verify_no_pii(dst)
            failed_checks = [k for k, v in checks.items() if not v]
            result["verification_checks"] = checks
            result["verification_failed"] = failed_checks

            if failed_checks:
                print(f"WARNING — residual PII signals: {failed_checks}")
                all_ok = False
            else:
                total = result["total_redactions"]
                print(f"OK ({total} redactions, {result['page_count']} pages)")
        else:
            print(f"FAILED — {result.get('error', result['status'])}")
            all_ok = False

    # Write report
    report_path = here / "REDACTION_REPORT.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nRedaction report written: {report_path}")

    if not all_ok:
        print("\nWARNING: Some redactions may need manual review. Check REDACTION_REPORT.json.")
        sys.exit(1)
    else:
        print("\nAll fixtures redacted and verified OK.")


if __name__ == "__main__":
    main()
