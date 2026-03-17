"""Admin Supabase Store — Supabase-first with in-memory fallback.

Writes incidents to the Supabase `incidents` table (migration 082).
Writes client events to the Supabase `client_events` table (migration 082).
Falls back to in-memory dicts when Supabase is unavailable.

Conforms to OpenAPI ops_telemetry_facade schemas.
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


# ---------------------------------------------------------------------------
# Severity mapping: legacy sev1-sev4 <-> new critical/high/medium/low
# ---------------------------------------------------------------------------

_LEGACY_TO_DB_SEVERITY: dict[str, str] = {
    "sev1": "critical",
    "sev2": "high",
    "sev3": "medium",
    "sev4": "low",
}

_DB_TO_LEGACY_SEVERITY: dict[str, str] = {v: k for k, v in _LEGACY_TO_DB_SEVERITY.items()}


def _to_db_severity(legacy: str) -> str:
    """Convert sev1/sev2/sev3/sev4 to critical/high/medium/low."""
    return _LEGACY_TO_DB_SEVERITY.get(legacy, legacy)


def _to_legacy_severity(db_val: str) -> str:
    """Convert critical/high/medium/low to sev1/sev2/sev3/sev4."""
    return _DB_TO_LEGACY_SEVERITY.get(db_val, db_val)


# ---------------------------------------------------------------------------
# Status mapping: legacy state <-> DB status
# ---------------------------------------------------------------------------

_LEGACY_STATE_TO_STATUS: dict[str, str] = {
    "open": "open",
    "investigating": "investigating",
    "mitigated": "resolved",
    "closed": "dismissed",
}

_STATUS_TO_LEGACY_STATE: dict[str, str] = {
    "open": "open",
    "investigating": "investigating",
    "resolved": "mitigated",
    "dismissed": "closed",
}


def _to_db_status(legacy_state: str) -> str:
    return _LEGACY_STATE_TO_STATUS.get(legacy_state, legacy_state)


def _to_legacy_state(db_status: str) -> str:
    return _STATUS_TO_LEGACY_STATE.get(db_status, db_status)


def _db_row_to_incident(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a Supabase incidents row to the legacy in-memory incident format."""
    return {
        "incident_id": row.get("id", ""),
        "state": _to_legacy_state(row.get("status", "open")),
        "severity": _to_legacy_severity(row.get("severity", "medium")),
        "title": row.get("title", ""),
        "correlation_id": row.get("correlation_id", ""),
        "trace_id": row.get("metadata", {}).get("trace_id") or row.get("correlation_id", ""),
        "suite_id": row.get("tenant_id"),
        "first_seen": row.get("created_at", ""),
        "last_seen": row.get("updated_at", ""),
        "fingerprint": row.get("fingerprint", ""),
        "timeline": row.get("metadata", {}).get("timeline", []),
        "evidence_pack": {
            "source": row.get("source", ""),
            "component": row.get("component", ""),
            "description": row.get("description", ""),
            "stack_trace": row.get("stack_trace", ""),
            "provider": row.get("provider", ""),
            **(row.get("metadata", {}).get("evidence_pack", {})),
        },
        "agent": row.get("metadata", {}).get("agent"),
        # Preserve raw DB fields for callers that need them
        "_db_id": row.get("id"),
        "_db_status": row.get("status"),
        "_db_severity": row.get("severity"),
    }


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

    # ------------------------------------------------------------------
    # Incident: store (new insert)
    # ------------------------------------------------------------------

    def store_incident(
        self,
        *,
        incident_id: str | None = None,
        tenant_id: str,
        title: str,
        severity: str,
        source: str = "backend",
        status: str = "open",
        description: str | None = None,
        stack_trace: str | None = None,
        component: str | None = None,
        provider: str | None = None,
        fingerprint: str | None = None,
        correlation_id: str | None = None,
        tags: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Insert a new incident into Supabase `incidents` table.

        Returns (row_dict, supabase_succeeded).
        Always stores in-memory as fallback.
        """
        db_status = _to_db_status(status) if status else "open"
        row = {
            "tenant_id": tenant_id,
            "title": title,
            "severity": _to_db_severity(severity),
            "source": source,
            "status": db_status,
        }
        if description:
            row["description"] = description
        if stack_trace:
            row["stack_trace"] = stack_trace
        if component:
            row["component"] = component
        if provider:
            row["provider"] = provider
        if fingerprint:
            row["fingerprint"] = fingerprint
        if correlation_id:
            row["correlation_id"] = correlation_id
        if tags is not None:
            row["tags"] = tags
        if metadata is not None:
            row["metadata"] = metadata

        client = _get_supabase()
        if client:
            try:
                result = client.table("incidents").insert(row).execute()
                if result.data:
                    db_row = result.data[0]
                    # Sync to in-memory
                    incident = _db_row_to_incident(db_row)
                    self._incidents[incident["incident_id"]] = incident
                    return db_row, True
            except Exception as e:
                logger.warning("AdminStore: Failed to store incident in Supabase: %s", e)

        # Fallback: in-memory only
        import uuid
        fallback_id = incident_id or str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        fallback_incident = {
            "incident_id": fallback_id,
            "state": status or "open",
            "severity": severity,
            "title": title,
            "correlation_id": correlation_id or "",
            "trace_id": (metadata or {}).get("trace_id") or correlation_id or "",
            "suite_id": tenant_id,
            "first_seen": now_iso,
            "last_seen": now_iso,
            "fingerprint": fingerprint or "",
            "timeline": (metadata or {}).get("timeline", []),
            "evidence_pack": {
                "source": source,
                "component": component or "",
                "description": description or "",
            },
        }
        self._incidents[fallback_id] = fallback_incident
        return fallback_incident, False

    # ------------------------------------------------------------------
    # Incident: update status
    # ------------------------------------------------------------------

    def update_incident(
        self,
        incident_id: str,
        *,
        status: str | None = None,
        resolved_by: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Update an incident's status/resolved_by in Supabase.

        Returns (updated_row, supabase_succeeded).
        """
        updates: dict[str, Any] = {}
        if status:
            updates["status"] = _to_db_status(status)
        if resolved_by:
            updates["resolved_by"] = resolved_by

        if not updates:
            return None, False

        client = _get_supabase()
        if client:
            try:
                result = (
                    client.table("incidents")
                    .update(updates)
                    .eq("id", incident_id)
                    .execute()
                )
                if result.data:
                    db_row = result.data[0]
                    incident = _db_row_to_incident(db_row)
                    self._incidents[incident["incident_id"]] = incident
                    return db_row, True
            except Exception as e:
                logger.warning("AdminStore: Failed to update incident %s: %s", incident_id, e)

        # Fallback: update in-memory
        mem_incident = self._incidents.get(incident_id)
        if mem_incident and status:
            mem_incident["state"] = _to_legacy_state(_to_db_status(status))
            if resolved_by:
                mem_incident["resolved_by"] = resolved_by
            return mem_incident, False

        return None, False

    # ------------------------------------------------------------------
    # Incident: upsert by fingerprint
    # ------------------------------------------------------------------

    def upsert_incident(
        self,
        *,
        tenant_id: str,
        title: str,
        severity: str,
        source: str = "backend",
        description: str | None = None,
        stack_trace: str | None = None,
        component: str | None = None,
        provider: str | None = None,
        fingerprint: str | None = None,
        correlation_id: str | None = None,
        tags: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool, bool]:
        """Upsert an incident using fingerprint-based dedup.

        If an open incident with the same fingerprint exists, updates it.
        Otherwise inserts a new one.

        Returns (incident_dict, deduped, supabase_succeeded).
        """
        client = _get_supabase()
        if client and fingerprint:
            try:
                row = {
                    "tenant_id": tenant_id,
                    "title": title,
                    "severity": _to_db_severity(severity),
                    "source": source,
                    "status": "open",
                }
                if description:
                    row["description"] = description
                if stack_trace:
                    row["stack_trace"] = stack_trace
                if component:
                    row["component"] = component
                if provider:
                    row["provider"] = provider
                if fingerprint:
                    row["fingerprint"] = fingerprint
                if correlation_id:
                    row["correlation_id"] = correlation_id
                if tags is not None:
                    row["tags"] = tags
                if metadata is not None:
                    row["metadata"] = metadata

                # Use the unique partial index on (fingerprint) WHERE status='open'
                # ON CONFLICT → update the existing open incident
                result = (
                    client.table("incidents")
                    .upsert(
                        row,
                        on_conflict="fingerprint",
                        # Only conflict on the partial unique index for open incidents
                    )
                    .execute()
                )
                if result.data:
                    db_row = result.data[0]
                    incident = _db_row_to_incident(db_row)
                    self._incidents[incident["incident_id"]] = incident
                    # Detect dedup: if created_at != updated_at, it was an update
                    deduped = db_row.get("created_at") != db_row.get("updated_at")
                    return incident, deduped, True
            except Exception as e:
                logger.warning("AdminStore: Supabase upsert_incident failed: %s", e)

        # Fallback: in-memory fingerprint dedup
        if fingerprint:
            for existing in self._incidents.values():
                if (
                    existing.get("fingerprint") == fingerprint
                    and existing.get("state") in {"open", "investigating", "mitigated"}
                ):
                    # Update existing
                    now_iso = datetime.now(timezone.utc).isoformat()
                    existing["title"] = title or existing.get("title", "")
                    existing["last_seen"] = now_iso
                    existing["correlation_id"] = correlation_id or existing.get("correlation_id", "")
                    # Update trace_id from metadata
                    if metadata and metadata.get("trace_id"):
                        existing["trace_id"] = metadata["trace_id"]
                    # Merge timeline from metadata
                    if metadata and "timeline" in metadata:
                        existing_timeline = existing.get("timeline", [])
                        existing_timeline.extend(metadata["timeline"])
                        existing["timeline"] = existing_timeline
                    return existing, True, False

        # No dedup match — insert new
        result_row, supabase_ok = self.store_incident(
            tenant_id=tenant_id,
            title=title,
            severity=severity,
            source=source,
            description=description,
            stack_trace=stack_trace,
            component=component,
            provider=provider,
            fingerprint=fingerprint,
            correlation_id=correlation_id,
            tags=tags,
            metadata=metadata,
        )
        return result_row or {}, False, supabase_ok

    # ------------------------------------------------------------------
    # Client events
    # ------------------------------------------------------------------

    def store_client_event(
        self,
        *,
        tenant_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        event_type: str,
        source: str = "desktop",
        severity: str = "info",
        component: str | None = None,
        page_route: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Write a client event to Supabase `client_events` table.

        Returns True if Supabase write succeeded.
        """
        row: dict[str, Any] = {
            "event_type": event_type,
            "source": source,
            "severity": severity,
        }
        if tenant_id:
            row["tenant_id"] = tenant_id
        if session_id:
            row["session_id"] = session_id
        if correlation_id:
            row["correlation_id"] = correlation_id
        if component:
            row["component"] = component
        if page_route:
            row["page_route"] = page_route
        if data is not None:
            row["data"] = data

        client = _get_supabase()
        if not client:
            logger.debug("AdminStore: No Supabase client — client_event dropped: %s", event_type)
            return False

        try:
            client.table("client_events").insert(row).execute()
            return True
        except Exception as e:
            logger.warning("AdminStore: Failed to store client_event: %s", e)
            return False

    # ------------------------------------------------------------------
    # Incident: query (Supabase-first, in-memory fallback)
    # ------------------------------------------------------------------

    def query_incidents(
        self,
        *,
        state: str | None = None,
        severity: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict], dict]:
        """Query incidents. Returns (items, page_info)."""
        client = _get_supabase()
        if client:
            try:
                query = client.table("incidents").select("*").order("created_at", desc=True)
                if state:
                    query = query.eq("status", _to_db_status(state))
                if severity:
                    query = query.eq("severity", _to_db_severity(severity))
                query = query.limit(limit + 1)

                result = query.execute()
                rows = result.data or []
                has_more = len(rows) > limit
                if has_more:
                    rows = rows[:limit]

                items = [_db_row_to_incident(r) for r in rows]
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
                result = (
                    client.table("incidents")
                    .select("*")
                    .eq("id", incident_id)
                    .single()
                    .execute()
                )
                if result.data:
                    return _db_row_to_incident(result.data)
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
