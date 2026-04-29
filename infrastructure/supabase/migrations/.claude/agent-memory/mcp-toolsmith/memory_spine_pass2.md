---
name: Memory Spine V1 Pass 2 — Codebase Patterns
description: Key findings from implementing memory_v1.py, memory_service.py, entity_thread_resolver.py — embedding dims, async patterns, receipt conventions
type: project
---

## Embedding dimensions

The task spec note saying `vector(1536)` was wrong. Migration 096 (`memory_objects.embedding`) is `vector(3072)`, matching `settings.embedding_dimensions = 3072`. The embed function from `legal_embedding_service.embed_text` uses `settings.embedding_dimensions` automatically. **Always use 3072 for memory_objects.**

**Why:** HNSW index on memory_objects was built with m=24 ef_construction=128 for 3072-dim vectors. Using 1536 would silently store truncated vectors that would fail cosine similarity comparisons with the index.

**How to apply:** When writing Pydantic validators for embedding fields on memory_objects, validate len == 3072 not 1536. The 1536 figure appears in the legal_embedding_service.py docstring only as a stale comment.

## Supabase client async pattern

All async service code uses module-level functions from `supabase_client.py`:
- `supabase_insert(table, row_dict)` → returns single row dict
- `supabase_select(table, filter_str, order_by=, limit=)` → returns list[dict]
- `supabase_update(table, match_filter_str, patch_dict)` → returns single row dict

Never instantiate a class — these are module-level helpers backed by a shared httpx.AsyncClient pool. Mirror this pattern exactly in new services.

## Receipt conventions

`store_receipts([receipt_dict])` from `receipt_store.py` is synchronous (the async writer runs in the background). Call it after every state-changing DB operation. Receipt dict must include at minimum:
- `id`, `receipt_type`, `tenant_id`, `suite_id`, `office_id`
- `actor_type` (must be "USER", "SYSTEM", or "WORKER")
- `action_type`, `tool_used`, `risk_tier`
- `trace_id`, `correlation_id`
- `redacted_inputs`, `redacted_outputs` (dicts — never put PII here)
- `outcome` ("success"/"failed"/"denied"), `reason_code`, `created_at`

No JSON schema file needed for new receipt types — the registry passes unknown types through in warn mode.

## TEXT vs UUID PKs in link tables

- `approval_requests.approval_id` is **TEXT PK** → use `str` in Python models, not UUID
- `receipts.receipt_id` is **TEXT PK** → use `str` in Python models
- `threads.thread_id`, `memory_objects.memory_id` are **UUID PKs** → use UUID

Cross-table joins from link tables must cast `::text` in raw SQL for tenant_id comparisons (tenant_memberships.tenant_id is TEXT, memory_objects.tenant_id is UUID).

## Idempotency conflict detection pattern

When `supabase_insert` raises `SupabaseClientError`, check:
- `exc.status_code == 409` OR `"23505" in exc.detail.lower()` OR `"unique" in exc.detail.lower()`

PostgREST may return 409 or embed the PG error code 23505 in the detail string. Check both.

## EntityThreadResolver SELECT-then-INSERT pattern

No UNIQUE constraint exists on (tenant_id, suite_id, canonical_entity_type, canonical_entity_id) in threads table — adding one requires a migration (out of scope for Pass 2). The resolver uses SELECT-first, INSERT-on-miss, SELECT-again-on-conflict to handle concurrent insert races without a migration.

## Test mock targets

When patching supabase functions in tests, patch the **import path in the target module**, not the source module:
- `aspire_orchestrator.services.memory_service.supabase_insert` (not `aspire_orchestrator.services.supabase_client.supabase_insert`)
- `aspire_orchestrator.services.memory_service.store_receipts` (not `aspire_orchestrator.services.receipt_store.store_receipts`)

## schemas/ directory structure

`backend/orchestrator/src/aspire_orchestrator/schemas/` contains:
- `__init__.py`, `06_output_schema.json`, `receipt_event.schema.json`
- `contracts/` (capabilities, events, evidence, learning, receipts subdirs)
- `ops_receipts/` (JSON Schema files for receipt type validation)

New Pydantic models go in `schemas/memory_v1.py` — no `__init__.py` registration needed. Import directly.
