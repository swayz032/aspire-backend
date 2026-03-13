"""Supabase PostgREST client for the Aspire orchestrator.

Provides async httpx client for INSERT/SELECT/UPDATE/RPC calls to Supabase.
Used by approval_check (draft creation) and resume (draft execution).

Error handling: All operations raise SupabaseClientError on failure (fail-closed, Law #3).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0  # seconds
_DISABLED_VECTOR_RPCS: set[str] = set()


class SupabaseClientError(Exception):
    """Raised when a Supabase PostgREST operation fails."""

    def __init__(self, operation: str, status_code: int | None = None, detail: str = ""):
        self.operation = operation
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Supabase {operation} failed: status={status_code} {detail}")


# ---------------------------------------------------------------------------
# Connection Pools (singleton, lazy-init)
# ---------------------------------------------------------------------------
_async_pool: httpx.AsyncClient | None = None
_sync_pool: httpx.Client | None = None
_pool_lock = threading.Lock()


def _get_async_pool() -> httpx.AsyncClient:
    """Return (or create) the module-level async HTTP pool."""
    global _async_pool
    if _async_pool is None or _async_pool.is_closed:
        with _pool_lock:
            if _async_pool is None or _async_pool.is_closed:
                _async_pool = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=50,
                        max_keepalive_connections=20,
                        keepalive_expiry=30,
                    ),
                    timeout=_TIMEOUT,
                )
    return _async_pool


def _get_sync_pool() -> httpx.Client:
    """Return (or create) the module-level sync HTTP pool."""
    global _sync_pool
    with _pool_lock:
        if _sync_pool is None or _sync_pool.is_closed:
            _sync_pool = httpx.Client(
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=20,
                    keepalive_expiry=30,
                ),
                timeout=_TIMEOUT,
            )
    return _sync_pool


async def close_pools() -> None:
    """Shutdown connection pools. Called during app shutdown."""
    global _async_pool, _sync_pool
    if _async_pool is not None:
        await _async_pool.aclose()
        _async_pool = None
    with _pool_lock:
        if _sync_pool is not None:
            _sync_pool.close()
            _sync_pool = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    key = settings.supabase_service_role_key
    if not key:
        raise SupabaseClientError("auth", detail="Missing ASPIRE_SUPABASE_SERVICE_ROLE_KEY")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _base_url() -> str:
    url = settings.supabase_url
    if not url:
        raise SupabaseClientError("config", detail="Missing ASPIRE_SUPABASE_URL")
    return f"{url.rstrip(chr(47))}/rest/v1"


def _handle_response(resp: httpx.Response, operation: str) -> Any:
    """Validate response and return JSON. Raises SupabaseClientError on non-2xx."""
    if resp.status_code >= 400:
        detail = f"HTTP {resp.status_code}"
        try:
            body = resp.json()
            if isinstance(body, dict):
                code = str(body.get("code", "")).strip()
                message = str(body.get("message", "")).strip()
                hint = str(body.get("hint", "")).strip()
                details = str(body.get("details", "")).strip()
                parts = [p for p in [code, message, hint, details] if p]
                if parts:
                    detail = " | ".join(parts)
        except Exception:
            pass
        logger.error(
            "Supabase %s failed: status=%d body=%s",
            operation, resp.status_code, resp.text[:200],
        )
        raise SupabaseClientError(operation, resp.status_code, detail)
    try:
        return resp.json()
    except Exception as e:
        raise SupabaseClientError(operation, resp.status_code, f"Invalid JSON response: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def supabase_rpc(fn_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call a Supabase RPC function."""
    if fn_name in _DISABLED_VECTOR_RPCS:
        raise SupabaseClientError(
            f"rpc/{fn_name}",
            status_code=503,
            detail="RPC_DISABLED_VECTOR_MISMATCH",
        )
    url = f"{_base_url()}/rpc/{fn_name}"
    try:
        client = _get_async_pool()
        resp = await client.post(url, json=params, headers=_headers())
        if resp.status_code >= 400:
            # Fast-disable vector RPCs when remote function/operator signatures
            # are incompatible with current database extension schema.
            try:
                body = resp.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                code = str(body.get("code", "")).strip()
                message = str(body.get("message", "")).lower()
                if code == "42883" and "operator does not exist" in message and "<=>" in message:
                    _DISABLED_VECTOR_RPCS.add(fn_name)
                    logger.warning(
                        "Disabling RPC %s for current process due to vector operator mismatch (code=42883).",
                        fn_name,
                    )
        return _handle_response(resp, f"rpc/{fn_name}")
    except SupabaseClientError:
        raise
    except httpx.TimeoutException:
        raise SupabaseClientError(f"rpc/{fn_name}", detail="Request timed out")
    except httpx.ConnectError:
        raise SupabaseClientError(f"rpc/{fn_name}", detail="Connection failed")
    except Exception as e:
        raise SupabaseClientError(f"rpc/{fn_name}", detail=str(e))


async def supabase_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
    """INSERT a row into a Supabase table (async)."""
    url = f"{_base_url()}/{table}"
    try:
        client = _get_async_pool()
        resp = await client.post(url, json=data, headers=_headers())
        result = _handle_response(resp, f"insert/{table}")
        return result[0] if isinstance(result, list) and result else result
    except SupabaseClientError:
        raise
    except httpx.TimeoutException:
        raise SupabaseClientError(f"insert/{table}", detail="Request timed out")
    except httpx.ConnectError:
        raise SupabaseClientError(f"insert/{table}", detail="Connection failed")
    except Exception as e:
        raise SupabaseClientError(f"insert/{table}", detail=str(e))


def supabase_insert_sync(table: str, data: dict[str, Any]) -> dict[str, Any]:
    """INSERT a row into a Supabase table (synchronous).

    Use this when calling from sync contexts inside an async event loop
    (e.g., LangGraph sync nodes running under uvicorn).
    """
    url = f"{_base_url()}/{table}"
    try:
        client = _get_sync_pool()
        resp = client.post(url, json=data, headers=_headers())
        result = _handle_response(resp, f"insert/{table}")
        return result[0] if isinstance(result, list) and result else result
    except SupabaseClientError:
        raise
    except httpx.TimeoutException:
        raise SupabaseClientError(f"insert/{table}", detail="Request timed out")
    except httpx.ConnectError:
        raise SupabaseClientError(f"insert/{table}", detail="Connection failed")
    except Exception as e:
        raise SupabaseClientError(f"insert/{table}", detail=str(e))


def supabase_upsert_sync(
    table: str, data: dict[str, Any], on_conflict: str = ""
) -> dict[str, Any]:
    """UPSERT (insert or update on conflict) a row in a Supabase table (sync)."""
    url = f"{_base_url()}/{table}"
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    if on_conflict:
        url = f"{url}?on_conflict={on_conflict}"
    try:
        client = _get_sync_pool()
        resp = client.post(url, json=data, headers=headers)
        result = _handle_response(resp, f"upsert/{table}")
        return result[0] if isinstance(result, list) and result else result
    except SupabaseClientError:
        raise
    except httpx.TimeoutException:
        raise SupabaseClientError(f"upsert/{table}", detail="Request timed out")
    except httpx.ConnectError:
        raise SupabaseClientError(f"upsert/{table}", detail="Connection failed")
    except Exception as e:
        raise SupabaseClientError(f"upsert/{table}", detail=str(e))


async def supabase_select(
    table: str, filters: str | dict[str, Any], *, order_by: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """SELECT rows from a Supabase table with query string filters.

    Args:
        table: PostgREST table name.
        filters: Either a raw query string (e.g. "id=eq.123") or a dict
            of {column: value} pairs converted to column=eq.value filters.
        order_by: Optional PostgREST order clause (e.g. "created_at.desc").
        limit: Optional row limit.
    """
    if isinstance(filters, dict):
        from urllib.parse import quote
        filter_str = "&".join(f"{k}=eq." + quote(str(v), safe="") for k, v in filters.items())
    else:
        filter_str = filters

    parts = [filter_str]
    if order_by:
        parts.append(f"order={order_by}")
    if limit is not None:
        parts.append(f"limit={limit}")
    query_string = "&".join(p for p in parts if p)

    url = f"{_base_url()}/{table}?{query_string}"
    hdrs = _headers()
    hdrs.pop("Prefer", None)
    try:
        client = _get_async_pool()
        resp = await client.get(url, headers=hdrs)
        return _handle_response(resp, f"select/{table}")
    except SupabaseClientError:
        raise
    except httpx.TimeoutException:
        raise SupabaseClientError(f"select/{table}", detail="Request timed out")
    except httpx.ConnectError:
        raise SupabaseClientError(f"select/{table}", detail="Connection failed")
    except Exception as e:
        raise SupabaseClientError(f"select/{table}", detail=str(e))


async def supabase_insert_batch(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """INSERT multiple rows into a Supabase table (async batch).

    PostgREST supports batch insert by POSTing a JSON array.
    Returns all inserted rows.
    """
    if not rows:
        return []
    url = f"{_base_url()}/{table}"
    try:
        client = _get_async_pool()
        resp = await client.post(url, json=rows, headers=_headers(), timeout=30.0)
        result = _handle_response(resp, f"insert_batch/{table}")
        return result if isinstance(result, list) else [result] if result else []
    except SupabaseClientError:
        raise
    except httpx.TimeoutException:
        raise SupabaseClientError(f"insert_batch/{table}", detail="Request timed out")
    except httpx.ConnectError:
        raise SupabaseClientError(f"insert_batch/{table}", detail="Connection failed")
    except Exception as e:
        raise SupabaseClientError(f"insert_batch/{table}", detail=str(e))


async def supabase_update(table: str, match_filters: str, data: dict[str, Any]) -> dict[str, Any]:
    """PATCH (update) rows in a Supabase table matching filters."""
    url = f"{_base_url()}/{table}?{match_filters}"
    try:
        client = _get_async_pool()
        resp = await client.patch(url, json=data, headers=_headers())
        result = _handle_response(resp, f"update/{table}")
        return result[0] if isinstance(result, list) and result else result
    except SupabaseClientError:
        raise
    except httpx.TimeoutException:
        raise SupabaseClientError(f"update/{table}", detail="Request timed out")
    except httpx.ConnectError:
        raise SupabaseClientError(f"update/{table}", detail="Connection failed")
    except Exception as e:
        raise SupabaseClientError(f"update/{table}", detail=str(e))


async def supabase_upsert(
    table: str, data: dict[str, Any], on_conflict: str = ""
) -> dict[str, Any]:
    """UPSERT (insert or update on conflict) a row in a Supabase table.

    Uses PostgREST Prefer: resolution=merge-duplicates header.
    The on_conflict param specifies the unique constraint columns.
    """
    url = f"{_base_url()}/{table}"
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    if on_conflict:
        url = f"{url}?on_conflict={on_conflict}"
    try:
        client = _get_async_pool()
        resp = await client.post(url, json=data, headers=headers)
        result = _handle_response(resp, f"upsert/{table}")
        return result[0] if isinstance(result, list) and result else result
    except SupabaseClientError:
        raise
    except httpx.TimeoutException:
        raise SupabaseClientError(f"upsert/{table}", detail="Request timed out")
    except httpx.ConnectError:
        raise SupabaseClientError(f"upsert/{table}", detail="Connection failed")
    except Exception as e:
        raise SupabaseClientError(f"upsert/{table}", detail=str(e))
