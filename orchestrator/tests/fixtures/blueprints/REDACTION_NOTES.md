# Blueprint Fixture Redaction Notes

PII redaction policy applied to every fixture in this directory before commit. Source of truth: plan §9.1.

## Strip (always)
- Engineer / architect personal name
- Engineer P.E. number / license number
- Owner / client name
- Full street address (house number + street)

## Keep
- City + jurisdiction (needed for permit lookups + tariff jurisdictions)
- Sheet number, discipline, scale, revision
- Title block layout (visually preserved with redaction bars)

## Originals
Pre-redaction PDFs live in `originals/` which is git-ignored. Only redacted versions ship in the repo.

## Procedure
1. Drop original into `originals/`.
2. Run redaction (manual — Acrobat redact tool or `pdf-redactor`).
3. Verify with QA pass — no names, no full addresses, no license numbers.
4. Commit only the redacted file in the fixture root.
