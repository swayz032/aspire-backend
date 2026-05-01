---
name: Aspire Law Compliance Patterns
description: Verified-good and known-bad patterns for the 10 Aspire Laws, discovered during output-critic reviews
type: feedback
---

## Law #2 (Receipt for All) — GOOD PATTERN

Delegating receipt-cutting to `MemoryService.write` via `BaseIngestionAdapter.ingest()` at line 244 is the correct architectural pattern. No adapter can "forget" to cut a receipt because they never cut it themselves — the base class always does it. Reference: `base.py` in ingestion services.

## Law #3 (Fail Closed) — KNOWN BAD PATTERN

Conditional signature check: `if secret and not verify(...)` silently bypasses auth when the secret env var is unset. This pattern appears in sarah.py line 181. The correct pattern: if secret is not configured, fail with 500 (misconfigured) or 401 (reject all). Never treat missing secret as "skip auth".

## Law #5 (Capability Tokens) — KNOWN GAP

As of Pass 18 (2026-04-29): The session broker mints cap tokens correctly. BUT: the Sarah personalization webhook does NOT verify a cap token — it relies on EL's HMAC signature only. This is an accepted-risk gap that needs formal security reviewer sign-off at each SHIP gate review. Do not let it silently pass.

## Law #6 (Tenant Isolation) — GOOD PATTERN

`FORCE ROW LEVEL SECURITY` on every new table + service_role bypass policy pattern is consistent and correct across migrations 095-103. Reference this pattern for any new table review.

## Law #9 (PII in logs) — GOOD PATTERN

call_ingestion.py truncates TranscriptionText to 80 chars in debug logs. sarah.py truncates caller_id to first 6 digits. These are the canonical examples for how to redact while preserving debuggability. Use as reference.

## AvaOrbVideo vs AvaOrb on dark backgrounds

Not a Law, but a recurring design defect: `AvaOrbVideo` has opaque MP4 backing that creates a black square/circle on dark backgrounds. The fix is `AvaOrb` (procedural, true alpha). This defect was found in BOTH MemoryEngineHero (fixed) AND FrontDeskSetupHero (NOT fixed as of Pass 18). Always grep for `AvaOrbVideo` in any new component on a dark-canvas surface.
