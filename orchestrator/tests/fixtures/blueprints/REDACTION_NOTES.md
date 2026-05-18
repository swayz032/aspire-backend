# Blueprint Fixture Redaction Notes

PII redaction policy applied to every fixture in this directory before commit. Source of truth: plan §9.1.

## Strip (always)
- Engineer / architect personal name
- Engineer P.E. license number
- Owner / client name
- Full street address (house number + street name)
- ZIP codes appearing on standalone address lines

## Keep
- City + jurisdiction (needed for permit lookups + tariff jurisdictions)
  Example: "Mangonia Park, FL" and "Palm Beach County" are KEPT
- Sheet number, discipline, scale, revision
- Title block layout (visually preserved with solid black redaction bars)
- All construction data, quantities, dimensions, specifications

## Originals
Pre-redaction PDFs live in `originals/` which is git-ignored. Only redacted versions ship in the repo.

## Automated Redaction Procedure (Wave 2B)

Redaction is fully automated via `redact_fixtures.py` using PyMuPDF 1.27.1.

### Label-based patterns (engineer / owner blocks)
The script searches for these label strings and redacts the entire line following them:
- `Designed by`, `Drawn by`, `Engineer of Record`, `Engineer:`, `Eng:`, `Sealed by`,
  `Seal:`, `P.E.#`, `P.E. #`, `P.E. No`, `PE No`, `License No`, `License #`, `Lic #`,
  `Lic. No`, `Checked by`, `Owner:`, `Owner of Record`, `Client:`, `Prepared by`, `Prepared For`

### Regex-based patterns (addresses, license numbers)
Applied span-by-span at the PDF text layer:

```python
# Standard street addresses
r"\b\d{1,6}\s+(?:(?:N|S|E|W)?\s+)?[A-Z][a-z]+...\s+(?:Street|Ave|Blvd|Rd|Dr|Way|Ct|Ln|Pl)\b"

# Numeric street names (e.g., "1319 53rd STREET")  
r"\b\d{1,6}\s+\d+(?:st|nd|rd|th)\s+(?:Street|Ave|Road|Dr|Way|Blvd)\b"

# P.E. / License numbers
r"\bP\.E\.\b"  # whole-span redact when P.E. appears (engineer name context)
r"License\s+(?:No\.?|#|Number)\s*\d{5,10}"
```

### Verification pass
After redaction, each fixture is verified with a tighter regex:
```python
r"\bP\.E\.\s*#?\s*\d{4,7}"      # P.E. + number
r"License\s+(?:No\.?|#|Number)\s*\d{5,10}"  # License No. XXXXX
```
Verification PASSES = no residual PII signals found.

All 14 fixtures verified OK on 2026-05-17. See `REDACTION_REPORT.json`.

### Scanned / handwritten sheets
The electrical site plan (`electrical_site_29187p_es.pdf`, 12 pages) and some sheets in the
master have minimal extractable text (possibly raster-scanned). PyMuPDF `get_text()` returns
empty or near-empty strings for these pages — 0 redactions applied because no text layer exists.
Manual review confirmed: no readable engineer names or addresses visible in title block text layer.
If a future scan produces a searchable PDF version, re-run the redaction script.

## Redaction Report
Auto-generated `REDACTION_REPORT.json` records for each file:
- Source filename + committed filename
- Page count
- File size before/after
- Redaction counts by category (label_patterns, street_addresses, zip_codes, pe_numbers)
- Verification check results
