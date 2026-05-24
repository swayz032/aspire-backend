---
name: wave-5-4-pass1-patterns
description: Wave 5.4 Pass 1 blocker patterns — _emit_receipt orphan persistence, module-level provider coroutines, Windows pytest collection hang
metadata:
  type: project
---

## _emit_receipt() vs store_receipts() — Law #2 gap pattern

`_emit_receipt()` in `adam_research.py` builds a receipt dict in memory but does NOT persist it.
You must separately call `store_receipts([receipt])` after `_emit_receipt()` to satisfy Law #2.
Affected methods: `subcontractor_discovery`, `labor_rate_lookup` (fixed 2026-05-24).

**Why:** `_emit_receipt` is a builder, not a persister. Compare to `_store_receipt` in `adam_supplier_router.py` which calls `store_receipts` internally. This naming asymmetry is a recurring trap.

**How to apply:** Whenever you see `_emit_receipt()` in adam_research skillpack methods, always check for a subsequent `store_receipts([receipt])` call. If missing, add it.

## Module-level provider coroutines vs instance methods

Unwrangle clients (Ferguson, Lowes, HD) expose:
- CLASS: `UnwrangleFergusonClient`, `UnwrangleLowesClient` (BaseProviderClient subclasses)
- MODULE-LEVEL coroutines: `search_products(line_item, *, suite_id, office_id, correlation_id, ...)` — these are the correct call target

Stubs that call `client = UnwrangleFergusonClient(); await client.search_products(line_item)` get
`AttributeError` at runtime (instance has no `search_products` — it's a module function).

Correct pattern:
```python
from aspire_orchestrator.providers import unwrangle_ferguson_client as _ferg
result = await _ferg.search_products(line_item, suite_id=..., office_id=..., correlation_id=...)
```

Lowes has separate `find_nearest_store(*, zipcode, suite_id, ...)` and `browse_category(*, category_url, store_no, suite_id, ...)` module-level coroutines. `store_no` is an `int` not `str`.

## Windows pytest collection hang — adam_research module

On this Windows dev environment, importing `aspire_orchestrator.skillpacks.adam_research` blocks
indefinitely. Root cause: top-level imports in that module pull in `search_router`, `tool_executor`,
`browser_service` which attempt network connections or blocking IO at import time.

Tests that import `AdamResearchSkillPack` or `AdamResearchContext` at module level (not inside test
functions) hang at pytest collection on Windows. This is a pre-existing constraint — not caused by
code changes. CI (Linux) runs cleanly.

Workaround: tests for supplier_router (which does not hang) can be run with:
`pytest tests/services/blueprint/test_adam_supplier_router.py -v` — 16/16 pass.

## Denial receipt on unknown-category branch (Law #2)

The `else` branch in `route_supplier_search()` was returning an error dict without a receipt.
Law #2 requires receipt on EVERY outcome including denials.

Pattern:
```python
else:
    _store_receipt(_make_receipt(
        event_type="adam.supplier_search.denied",
        ..., status="denied",
        metadata={"reason_code": "UNKNOWN_CATEGORY"},
    ))
    result = {"status": "error", ...}
```

## Degraded status when zero results (P-4 pattern)

Parent receipt should reflect actual quality:
```python
status = "degraded" if total_found == 0 else "ok"
```
Not hardcoded `"ok"`. This ensures receipts truthfully report search quality.

## InternalErrorCode taxonomy — PROVIDER_ prefix does NOT exist

`InternalErrorCode` lives in `providers/error_codes.py`. Categories are:
`AUTH_`, `NETWORK_`, `RATE_`, `INPUT_`, `SERVER_`, `DOMAIN_`, `TEMPLATE_`, `VALIDATION_`.

There is NO `PROVIDER_` prefix. Common mistake: writing `InternalErrorCode.PROVIDER_INTERNAL_ERROR`
or `InternalErrorCode.PROVIDER_UNAVAILABLE` — neither exists. Correct mappings:
- HTTP 500/503 → `InternalErrorCode.SERVER_INTERNAL_ERROR`
- HTTP 503 unavailable → `InternalErrorCode.SERVER_UNAVAILABLE`
- Timeout → `InternalErrorCode.NETWORK_TIMEOUT`
- Rate limit → `InternalErrorCode.RATE_LIMITED`
- Auth failure → `InternalErrorCode.AUTH_INVALID_KEY`

Also: `reason_code` string literals in receipt calls (e.g. `reason_code="PROVIDER_INTERNAL_ERROR"`)
are NOT validated by mypy but still need to match the enum values for consistency.
Always use `InternalErrorCode.XYZ.value` for reason_code strings, not bare string literals.

## hasattr guard pattern for stub provider clients

When a provider client is imported under a try/except availability flag and the stub method
hasn't been implemented yet, guard the call with `hasattr` rather than adding a fake method stub:

```python
if not hasattr(client, "search_products"):
    logger.warning("...: method not implemented — HD_NOT_IMPLEMENTED")
    return [], 0
```

This is safer than adding `def search_products(self): return []` because a fake method that
silently returns empty can mask future wiring bugs. The `hasattr` guard makes it explicit.

## Coverage target patterns for dual-account SerpApi clients

Both `serpapi_local_client.py` and `serpapi_local_service_ads_client.py` share the same
dual-account budget gate structure. To reach ≥80% coverage, you need tests for:
1. `select_account()` returning `None` → budget exhausted path
2. `try_increment()` returning `False` for both A and B → double-fail exhausted path
3. `get_api_key()` raising `KeyError` → AUTH_INVALID_KEY receipt
4. HTTP 429 response → `mark_account_exhausted()` + account swap retry path
5. HTTP 500 response → FAILED outcome with receipt

Tests 1-3 use monkeypatch on module-level budget functions. Test 4 requires a side_effect
that returns 429 on first call, success on second. Test 5 uses `_make_provider_response(False, ..., status=500)`.

## # noqa: F401 on conditional availability imports

Pattern for availability-detection imports that ruff flags as unused:
```python
try:
    from aspire_orchestrator.providers.unwrangle_ferguson_client import UnwrangleFergusonClient  # type: ignore[import]  # noqa: F401
    _UNWRANGLE_FERGUSON_AVAILABLE = True
except ImportError:
    _UNWRANGLE_FERGUSON_AVAILABLE = False
```

The import IS used — its success/failure sets the flag. `# noqa: F401` on the same line as
`# type: ignore[import]` works; put noqa after the type ignore comment.

## Branch/commit context

- Branch: `feat/wave-5.4-supplier-intelligence`
- Commit `d6dbbeb` — "fix(5.4): Pass 1 verification close blockers (receipts + stub mismatch)"
- Commit `17d39dc` — "fix(5.4): Pass 1 final blockers — runtime safety + coverage"
- PR #102 draft on swayz032/aspire-backend (ready for merge after 17d39dc)
