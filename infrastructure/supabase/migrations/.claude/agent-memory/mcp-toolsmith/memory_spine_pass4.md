---
name: Memory Spine V1 Pass 4 — Route Layer Patterns
description: Key findings from implementing memory.py + memory_pages.py FastAPI routes (Pass 4) — DI patterns, service call signatures, tenant isolation guards
type: project
---

## MemoryService.write call signature

`write(envelope: MemoryObjectIn, *, scope: ScopedIdentity, embed: bool = True)`

The `scope` parameter is REQUIRED as a keyword argument (it is NOT extracted from `envelope.scope` automatically — it's a defense-in-depth cross-check). Every route that calls `svc.write(obj_in)` must pass `scope=body.scope` explicitly.

## MemoryService.list_by_thread call signature and return type

`list_by_thread(thread_id: UUID, *, scope: ScopedIdentity, memory_types=None, limit=50, cursor=None) -> tuple[list[MemoryObjectOut], str | None]`

Returns a *tuple* `(items, next_cursor)` — NOT just a list. Always unpack: `objects, _ = await svc.list_by_thread(...)`.

## MemoryService.list_by_entity call signature

`list_by_entity(entity_type: str, entity_id: UUID, *, scope: ScopedIdentity, memory_types=None, limit=50) -> list[MemoryObjectOut]`

First two args are positional. Returns a plain list (no cursor). No tuple unpacking needed.

## FastAPI dependency injection for scope headers

Pattern used in all memory routes: a `get_scope(request: Request)` dependency that:
1. Reads X-Tenant-Id, X-Suite-Id, X-Office-Id, X-Actor-Id from headers
2. Validates all three required fields are present — raises 401 with `SCOPE_MISSING` if any missing
3. Parses UUIDs — raises 401 with `SCOPE_INVALID` if any non-UUID string
4. Emits a denial receipt via `store_receipts()` before raising (Law #2)
5. Returns `ScopedIdentityFromHeaders` dataclass

Tenant isolation cross-check: every route body that carries a `scope: ScopedIdentity` field must assert `str(scope_header.tenant_id) == str(body.scope.tenant_id)` and raise 403 `TENANT_ISOLATION_VIOLATION` on mismatch.

## Dependency override pattern for route tests

Route tests use `patch()` context managers at the module level of the route file, NOT `app.dependency_overrides`. This is because the routes instantiate service objects inline (not injected via Depends). Patch targets:
- `aspire_orchestrator.routes.memory.MemoryService`
- `aspire_orchestrator.routes.memory.BriefMaterializer`
- `aspire_orchestrator.routes.memory.ProactiveCandidateEngine`
- `aspire_orchestrator.routes.memory.TranscriptEventRefinery`
- `aspire_orchestrator.routes.memory.supabase_insert`
- `aspire_orchestrator.routes.memory.store_receipts`
- `aspire_orchestrator.routes.memory_pages.MemoryService`
- `aspire_orchestrator.routes.memory_pages.BriefMaterializer`

## BriefMaterializer.build_office_brief / build_finance_brief signature

`build_office_brief(office_id: UUID, scope: ScopedIdentity, refresh: bool = False)`
`build_finance_brief(office_id: UUID, scope: ScopedIdentity, refresh: bool = False)`
`build_thread_brief(thread_id: UUID, scope: ScopedIdentity)`

All async. Return `OfficeBriefOut`, `FinanceBriefOut`, `ThreadBriefOut` respectively (from schemas/memory_v1.py).

## supabase_insert for memory_event_inbox

`memory_event_inbox` does NOT have a server-generated event_id — pass `event_id` in the row dict. The route generates `event_id = uuid.uuid4()` before calling insert and uses it in the response.

## _kick_refinery_async pattern

Background refinery kick uses `asyncio.get_event_loop().create_task()` guarded by `loop.is_running()`. This avoids blocking the response. In tests, patch `aspire_orchestrator.routes.memory._kick_refinery_async` to prevent spurious background tasks from interfering with assertions.

## Route file structure confirmed

- `server.py` router imports are at lines ~106-111 (import block)
- `app.include_router()` calls are at lines ~224-241 (after CORS setup)
- New routers appended AFTER existing 4 routers
- Server had 72 routes before Pass 4; has 94 routes after (22 new)

## approval_service.create_approval_binding

The existing approval_service does NOT have a `create_request` method. The function is `create_approval_binding(suite_id, office_id, action_type, risk_tier, payload, correlation_id)` — returns an `ApprovalBinding` dataclass. This is what the `POST /v1/approvals/request` route calls, then separately writes the `approval_links` spine row.

## MemoryObjectIn field names

There is no `linked_memory_ids` field on `MemoryObjectIn`. The linkage fields are:
- `linked_receipt_ids: list[UUID]`
- `linked_approval_ids: list[UUID]`
- `linked_artifact_ids: list[UUID]`
- `linked_workflow_run_ids: list[UUID]`

## Pass 5 stub pattern

Routes that depend on `memory_search.MemorySearchService` (not yet built) return:
```python
MemorySearchResponse(results=[], total=0, note="Pass 5 ships memory_search — currently returning empty stub results.")
```
And log a structured INFO line with `(STUB — Pass 5 not shipped)` so the stub is grep-able. No TODO comment in the import block — the TODO is in the route handler docstring.
