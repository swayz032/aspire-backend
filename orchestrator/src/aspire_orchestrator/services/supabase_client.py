"""Supabase PostgREST client for the Aspire orchestrator.

Provides async httpx client for INSERT/SELECT/UPDATE/RPC calls to Supabase.
Used by approval_check (draft creation) and resume (draft execution).

Error handling: All operations raise SupabaseClientError on failure (fail-closed, Law #3).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0  # seconds


class SupabaseClientError(Exception):
    """Raised when a Supabase PostgREST operation fails."""

    def __init__(self, operation: str, status_code: int | None = None, detail: str = ""):
        self.operation = operation
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Supabase {operation} failed: status={status_code} {detail}")


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
    return f"{url.rstrip('/')}/rest/v1"


def _handle_response(resp: httpx.Response, operation: str) -> Any:
    """Validate response and return JSON. Raises SupabaseClientError on non-2xx."""
    if resp.status_code >= 400:
        # Never expose raw Supabase error details externally
        detail = f"HTTP {resp.status_code}"
        logger.error(
            "Supabase %s failed: status=%d body=%s",
            operation, resp.status_code, resp.text[:200],
        )
        raise SupabaseClientError(operation, resp.status_code, detail)
    try:
        return resp.json()
    except Exception as e:
        raise SupabaseClientError(operation, resp.status_code, f"Invalid JSON response: {e}")


async def supabase_rpc(fn_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call a Supabase RPC function."""
    url = f"{_base_url()}/rpc/{fn_name}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=params, headers=_headers())
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
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
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
        with httpx.Client(timeout=_TIMEOUT) as client:
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


async def supabase_select(table: str, filters: str) -> list[dict[str, Any]]:
    """SELECT rows from a Supabase table with query string filters."""
    url = f"{_base_url()}/{table}?{filters}"
    hdrs = _headers()
    hdrs.pop("Prefer", None)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
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
        async with httpx.AsyncClient(timeout=_TIMEOUT * 3) as client:  # Longer timeout for batches
            resp = await client.post(url, json=rows, headers=_headers())
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
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
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
