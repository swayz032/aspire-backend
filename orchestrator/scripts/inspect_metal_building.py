"""Quick inspection: confirm 29187P-ES is a metal building drawing.
Extract per-page text + dominant keywords to verify discipline category."""
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

PDF = Path(r"C:\Users\tonio\Projects\myapp\blueprints\29187P-ES (3).pdf")

METAL_BUILDING_PATTERNS = [
    r"\bMETAL\s+BUILDING\b", r"\bPRE[- ]ENGINEERED\b", r"\bSTEEL\s+FRAME\b",
    r"\bRIGID\s+FRAME\b", r"\bPURLIN\b", r"\bGIRT\b", r"\bRAFTER\b",
    r"\bEAVE\b", r"\bRIDGE\b", r"\bBASE\s+PLATE\b", r"\bANCHOR\s+BOLT\b",
    r"\bCOLD\s*FORMED\b", r"\bMBMA\b",  # Metal Building Manufacturers Association
    r"\bGABLE\b", r"\bDEAD\s+LOAD\b", r"\bSNOW\s+LOAD\b", r"\bWIND\s+LOAD\b",
    r"\bENDWALL\b", r"\bSIDEWALL\b", r"\bROOF\s+PANEL\b", r"\bWALL\s+PANEL\b",
    r"\bSTANDING\s+SEAM\b",
]

ELECTRICAL_SITE_PATTERNS = [
    r"\bELECTRICAL\b", r"\bELECTRICAL\s+SITE\b", r"\bPHOTOVOLTAIC\b",
    r"\bCONDUIT\b", r"\bPANEL\s+BOARD\b", r"\bSWITCHGEAR\b",
]

doc = fitz.open(str(PDF))
print(f"PDF: {PDF.name}")
print(f"pages: {doc.page_count}\n")

metal_total = 0
elec_total = 0
all_text = ""

for pg in range(doc.page_count):
    text = (doc[pg].get_text() or "").upper()
    all_text += text + "\n"

    metal_hits = 0
    elec_hits = 0
    metal_evidence = []
    elec_evidence = []
    for pat in METAL_BUILDING_PATTERNS:
        m = re.findall(pat, text)
        if m:
            metal_hits += len(m)
            metal_evidence.append(f"{pat}({len(m)})")
    for pat in ELECTRICAL_SITE_PATTERNS:
        m = re.findall(pat, text)
        if m:
            elec_hits += len(m)
            elec_evidence.append(f"{pat}({len(m)})")

    metal_total += metal_hits
    elec_total += elec_hits

    # Extract sheet code candidates
    sheet_codes = re.findall(r"\b([A-Z]{1,3}[-_]?\d{1,3}(?:\.\d{1,2})?)\b", text)
    sheet_code_top = Counter(sheet_codes).most_common(3)

    text_sample = text[:200].replace("\n", " ")
    print(f"--- Page {pg} ---")
    print(f"  text sample: {text_sample[:150]}...")
    print(f"  metal hits: {metal_hits}   {metal_evidence[:3]}")
    print(f"  elec hits:  {elec_hits}    {elec_evidence[:3]}")
    print(f"  sheet codes: {sheet_code_top}")
    print()

print(f"=== TOTAL ACROSS {doc.page_count} PAGES ===")
print(f"metal_building hits: {metal_total}")
print(f"electrical_site hits: {elec_total}")
print(f"\nverdict: {'METAL_BUILDING' if metal_total > elec_total else 'ELECTRICAL_SITE'}")

# Sample distinct keywords
print(f"\nTop 20 distinct construction keywords found:")
construction_words = re.findall(
    r"\b(METAL|BUILDING|STEEL|FRAME|PURLIN|GIRT|RAFTER|EAVE|RIDGE|PANEL|"
    r"PLATE|ANCHOR|BOLT|GABLE|LOAD|WALL|ROOF|CONCRETE|FOUNDATION|"
    r"ELECTRICAL|CONDUIT|PHOTO|SOLAR|ARRAY)\b",
    all_text
)
for kw, n in Counter(construction_words).most_common(20):
    print(f"  {kw:<20s} {n}")

doc.close()
