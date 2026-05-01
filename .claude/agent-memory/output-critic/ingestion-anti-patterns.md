---
name: Ingestion Adapter Anti-Patterns
description: Recurring mistakes found in Pass 14-18 ingestion adapter reviews
type: feedback
---

## Exception-as-control-flow (calendar_ingestion.py Pass 18)

Rule: Never raise `IngestionError` with `status_code=200` to signal a no-op.
**Why:** Sentry/Datadog treats any `Exception` as an error event. Routes that catch IngestionError and return `status_code` still emit `{ok:false}` JSON, which triggers provider retries.
**How to apply:** Early return a synthetic `IngestionResult` or raise a dedicated `IngestionNoOp` sentinel that the route maps to `{ok:true, code:"NO_OP"}`.

## Base class bypass in `ingest()` overrides

Rule: When an adapter overrides `ingest()`, it must preserve the base class security sequence: verify_signature → resolve_scope → thread_envelope → build_envelope → write.
**Why:** GoogleCalendarIngestionAdapter skipped thread_envelope, leaving all calendar events threadless. Future security posture changes to the base class (cap token checks) won't apply to bypassing adapters.
**How to apply:** Use composition (`event_payload` loop) rather than full override, or call `super().ingest(...)` for the common path.

## Missing thread resolution in overriding adapters

Rule: Adapters that override `ingest()` with per-item loops must call `thread_envelope()` per item, not pass `thread=None`.
**Why:** Thread-less memories don't group in search ranking. Recurring calendar events should share a thread keyed on `recurring_event_id`.

## as-any casts hiding data model errors (MemoryDetailInvoice)

Rule: `(memory as any).contact?.phone` used as an email field is a schema inconsistency buried under a type escape.
**Why:** Grep for `as any` in detail components and verify every field name matches the `MemoryDetail` schema. Phone used as email = silent wrong data.
