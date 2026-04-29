---
name: Aspire Infrastructure Patterns
description: Recurring SRE findings across Aspire backend reviews — timeouts, health probes, receipt patterns, RLS conventions
type: project
---

## Timeout Budget

`supabase_client._TIMEOUT = 10.0` (seconds) — this is the module-level HTTP timeout inherited by all DB-touching services. The Aspire Production Gates spec says tools must complete in <5s; the actual DB client timeout is 10s. This is a recurring gap. Do not assume a 5s budget is enforced — it is not.

**How to apply:** Flag `_TIMEOUT` value in every Gate 3 review. Note it as a conditional (not blocker) unless the service is a synchronous tool call in a user-facing path.

## Health Probe Coverage

`GET /readyz` checks: signing key, graph_built, DLP, receipt_store, Redis. It does NOT check:
- New Postgres tables added after the probe was written (e.g., spine tables in memory engine)
- Temporal worker connectivity
- Embedding API (OpenAI) reachability
- pg_cron job state

**How to apply:** Every PRR must verify `/readyz` covers the new service's critical dependencies. If it doesn't, flag as Gate 2 gap.

## RLS Convention

All Aspire spine tables use:
```sql
ALTER TABLE public.<name> ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.<name> FORCE ROW LEVEL SECURITY;
```
Both lines are required. A table with ENABLE but not FORCE allows service_role to bypass RLS — that's intentional for internal operations but must be verified against the plan spec per table.

## Receipt Store Architecture

`receipt_store.store_receipts([receipt_dict])` is the canonical write path (sync, fire-and-forget). It is imported by `memory_service.py`, `transcript_event_refinery.py`, `proactive_candidate_engine.py`. The `brief_materializer.py` does NOT emit receipts on its own upserts — it reads receipts as input data only. This is a known Law #2 gap in the V1 implementation.

## Migration Numbering

The plan spec and the actual migration files can have a numbering offset. In the Memory Engine V1 plan, spec called migrations 094-099 but files on disk are 095-100. Always verify by reading the file header comment (`-- Migration <N>: ...`) not just the filename.

## Capability Token Gap in New Routes

New routes added to `routes/memory.py` and `routes/memory_pages.py` use `get_scope()` for authentication (X-Tenant-Id / X-Suite-Id / X-Office-Id header injection) but do NOT verify `X-Capability-Token`. The capability token is minted by the session broker but not checked per-endpoint. This is a Law #5 gap present in the V1 memory routes. Flag in every Gate 5 review of new route files.

## DLP / PII Redaction Gap

`get_dlp_service()` (Presidio) is initialized in `readyz` and is available, but the memory write path (`routes/memory.py`, `memory_service.py`) does NOT call it before writing `title` and `summary` fields. This means user-generated text with PII is written raw to `memory_objects`. Flag as Law #9 gap in every review of new content-ingestion routes.
