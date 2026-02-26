"""Admin Supabase Store (Wave 2C — F2 fix).

Replaces empty in-memory dicts with real Supabase queries for admin ops.
Conforms to OpenAPI ops_telemetry_facade schemas.

Graceful degradation: try Supabase first, fall back to in-memory.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_supabase_client: Any = None
_supabase_init_done = False
_supabase_init_lock = threading.Lock()


def _get_supabase() -> Any | None:
    """Lazy-init Supabase client for admin store."""
    global _supabase_client, _supabase_init_done

    if _supabase_init_done:
        return _supabase_client

    with _supabase_init_lock:
        if _supabase_init_done:
            return _supabase_client

        url = os.environ.get("ASPIRE_SUPABASE_URL", "")
        key = os.environ.get("ASPIRE_SUPABASE_SERVICE_ROLE_KEY", "")

        if url and key:
            try:
                from supabase import create_client
                _supabase_client = create_client(url, key)
                logger.info("AdminStore: Supabase client initialized")
            except Exception as e:
                logger.warning("AdminStore: Supabase init failed: %s", e)
                _supabase_client = None
        else:
            logger.info("AdminStore: No Supabase config — in-memory only")
            _supabase_client = None

        _supabase_init_done = True
        return _supabase_client


class AdminSupabaseStore:
    """Supabase-backed store for admin ops with in-memory fallback."""

    def __init__(
        self,
        incidents: dict[str, dict],
        provider_calls: list[dict],
    ):
        """Initialize with references to in-memory stores for fallback."""
        self._incidents = incidents
        self._provider_calls = provider_calls

    def store_incident(self, incident: dict) -> bool:
        """Store incident to Supabase + in-memory. Returns True if Supabase succeeded."""
        # Always store in-memory (fast, guaranteed)
        incident_id = incident.get("incident_id", "")
        self._incidents[incident_id] = incident

        # Try Supabase
        client = _get_supabase()
        if not client:
            return False

        try:
            row = {
                "incident_id": incident_id,
                "suite_id": incident.get("suite_id", "system"),
                "state": incident.get("state", "open"),
                "severity": incident.get("severity", "medium"),
                "title": incident.get("title", ""),
                "correlation_id": incident.get("correlation_id"),
                "first_seen": incident.get("first_seen"),
                "last_seen": incident.get("last_seen"),
                "timeline": incident.get("timeline", []),
                "evidence_pack": incident.get("evidence_pack", {}),
            }
            client.table("incidents").insert(row).execute()
            return True
        except Exception as e:
            logger.warning("AdminStore: Failed to store incident: %s", e)
            return False

    def query_incidents(
        self,
        *,
        state: str | None = None,
        severity: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict], dict]:
        """Query incidents. Returns (items, page_info)."""
        # Try Supabase first
        client = _get_supabase()
        if client:
            try:
                query = client.table("incidents").select("*").order("first_seen", desc=True)
                if state:
                    query = query.eq("state", state)
                if severity:
                    query = query.eq("severity", severity)
                query = query.limit(limit + 1)  # +1 to check has_more

                result = query.execute()
                items = result.data or []
                has_more = len(items) > limit
                if has_more:
                    items = items[:limit]

                page_info = {
                    "has_more": has_more,
                    "next_cursor": items[-1]["incident_id"] if has_more and items else None,
                }
                return items, page_info
            except Exception as e:
                logger.warning("AdminStore: Supabase query failed, falling back: %s", e)

        # Fallback to in-memory with cursor-based pagination
        items = list(self._incidents.values())
        if state:
            items = [i for i in items if i.get("state") == state]
        if severity:
            items = [i for i in items if i.get("severity") == severity]
        items.sort(key=lambda x: x.get("last_seen", x.get("first_seen", "")), reverse=True)

        # Apply cursor-based pagination
        start = 0
        if cursor:
            for idx, item in enumerate(items):
                if item.get("incident_id") == cursor:
                    start = idx + 1
                    break

        page = items[start: start + limit]
        has_more = start + limit < len(items)
        next_cursor = page[-1]["incident_id"] if has_more and page else None

        return page, {"has_more": has_more, "next_cursor": next_cursor}

    def get_incident(self, incident_id: str) -> dict | None:
        """Get a single incident by ID."""
        client = _get_supabase()
        if client:
            try:
                result = client.table("incidents").select("*").eq("incident_id", incident_id).single().execute()
                return result.data
            except Exception:
                pass

        return self._incidents.get(incident_id)

    def query_provider_calls(
        self,
        *,
        provider: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict], dict]:
        """Query provider calls. Returns (items, page_info)."""
        client = _get_supabase()
        if client:
            try:
                query = client.table("provider_call_log").select("*").order("started_at", desc=True)
                if provider:
                    query = query.eq("provider", provider)
                if status:
                    query = query.eq("status", status)
                query = query.limit(limit + 1)

                result = query.execute()
                items = result.data or []
                has_more = len(items) > limit
                if has_more:
                    items = items[:limit]

                return items, {"has_more": has_more, "next_cursor": items[-1]["call_id"] if has_more and items else None}
            except Exception as e:
                logger.warning("AdminStore: Supabase provider_calls query failed: %s", e)

        # Fallback to in-memory provider call logger
        try:
            from aspire_orchestrator.services.provider_call_logger import get_provider_call_logger
            pcl = get_provider_call_logger()
            items = pcl.query_calls(provider=provider, status=status, limit=limit)
            return items, {"has_more": False, "next_cursor": None}
        except Exception:
            return self._provider_calls[:limit], {"has_more": False, "next_cursor": None}


# Module singleton
_store_instance: AdminSupabaseStore | None = None


def get_admin_store(
    incidents: dict[str, dict] | None = None,
    provider_calls: list[dict] | None = None,
) -> AdminSupabaseStore:
    """Get the singleton AdminSupabaseStore."""
    global _store_instance
    if _store_instance is None:
        _store_instance = AdminSupabaseStore(
            incidents=incidents or {},
            provider_calls=provider_calls or [],
        )
    return _store_instance
