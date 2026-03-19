"""SRE Triage Background Task — Phase 5C Observability.

Runs every 5 minutes. Checks for:
1. Receipt failure spikes (>=5 FAILED receipts in 5 min window) -> auto-incident
2. Provider call failure spikes (>=3 failures per provider in 5 min) -> per-provider incident

Uses fingerprint-based dedup via admin_store.upsert_incident() to avoid spam.
Best-effort: if Supabase is unavailable, logs warning and skips the cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INTERVAL_SECONDS = 300  # 5 minutes
_RECEIPT_FAILURE_THRESHOLD = 5
_RECEIPT_CRITICAL_THRESHOLD = 10
_PROVIDER_FAILURE_THRESHOLD = 3
_SYSTEM_TENANT_ID = "system"

# Fingerprints for dedup
_RECEIPT_BREACH_FINGERPRINT = "sre-slo-breach-5min"
_PROVIDER_BREACH_FINGERPRINT_PREFIX = "sre-provider-"
_PROVIDER_BREACH_FINGERPRINT_SUFFIX = "-breach"

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_triage_task: asyncio.Task[None] | None = None
_shutdown_event: asyncio.Event | None = None


# ---------------------------------------------------------------------------
# Supabase helper (reuses admin_store pattern)
# ---------------------------------------------------------------------------

def _get_supabase() -> Any | None:
    """Lazy-init Supabase client, delegating to admin_store."""
    from aspire_orchestrator.services.admin_store import _get_supabase as _admin_get_sb
    return _admin_get_sb()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _query_receipt_failures(client: Any, since_iso: str) -> int:
    """Count FAILED receipts in the window. Returns count or 0 on error."""
    try:
        result = (
            client.table("receipts")
            .select("receipt_id", count="exact")
            .eq("status", "FAILED")
            .gte("created_at", since_iso)
            .execute()
        )
        return result.count if result.count is not None else 0
    except Exception as e:
        logger.warning("SRE triage: receipt query failed: %s", e)
        return 0


def _query_provider_failures(client: Any, since_iso: str) -> dict[str, int]:
    """Count failed provider calls grouped by provider. Returns {provider: count}."""
    try:
        result = (
            client.table("provider_call_log")
            .select("external_provider")
            .eq("status", "failed")
            .gte("started_at", since_iso)
            .execute()
        )
        rows = result.data or []
        counts: dict[str, int] = {}
        for row in rows:
            prov = row.get("external_provider", "unknown")
            counts[prov] = counts.get(prov, 0) + 1
        return counts
    except Exception as e:
        logger.warning("SRE triage: provider_call_log query failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Incident creation helpers
# ---------------------------------------------------------------------------

def _create_receipt_breach_incident(failure_count: int) -> None:
    """Create/upsert incident for receipt failure SLO breach."""
    from aspire_orchestrator.services.admin_store import get_admin_store

    severity = "critical" if failure_count >= _RECEIPT_CRITICAL_THRESHOLD else "high"
    store = get_admin_store()
    incident, deduped, sb_ok = store.upsert_incident(
        tenant_id=_SYSTEM_TENANT_ID,
        title=f"SLO Breach: {failure_count} failures in 5 minutes",
        severity=severity,
        source="sre",
        component="sre_triage",
        fingerprint=_RECEIPT_BREACH_FINGERPRINT,
        description=(
            f"Automated SRE triage detected {failure_count} FAILED receipts "
            f"in the last 5-minute window. Threshold: {_RECEIPT_FAILURE_THRESHOLD}."
        ),
        metadata={
            "failure_count": failure_count,
            "threshold": _RECEIPT_FAILURE_THRESHOLD,
            "window_seconds": _INTERVAL_SECONDS,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    action = "deduped" if deduped else "created"
    backend = "supabase" if sb_ok else "in-memory"
    logger.info(
        "SRE triage: receipt breach incident %s (%s, severity=%s, count=%d, backend=%s)",
        action, incident.get("incident_id", "?"), severity, failure_count, backend,
    )


def _create_provider_breach_incident(provider: str, failure_count: int) -> None:
    """Create/upsert incident for provider failure spike."""
    from aspire_orchestrator.services.admin_store import get_admin_store

    fingerprint = (
        f"{_PROVIDER_BREACH_FINGERPRINT_PREFIX}{provider}"
        f"{_PROVIDER_BREACH_FINGERPRINT_SUFFIX}"
    )
    store = get_admin_store()
    incident, deduped, sb_ok = store.upsert_incident(
        tenant_id=_SYSTEM_TENANT_ID,
        title=f"Provider Failure Spike: {provider} ({failure_count} failures in 5 min)",
        severity="high",
        source="sre",
        component="sre_triage",
        provider=provider,
        fingerprint=fingerprint,
        description=(
            f"Automated SRE triage detected {failure_count} failed calls to "
            f"provider '{provider}' in the last 5-minute window. "
            f"Threshold: {_PROVIDER_FAILURE_THRESHOLD}."
        ),
        metadata={
            "provider": provider,
            "failure_count": failure_count,
            "threshold": _PROVIDER_FAILURE_THRESHOLD,
            "window_seconds": _INTERVAL_SECONDS,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    action = "deduped" if deduped else "created"
    backend = "supabase" if sb_ok else "in-memory"
    logger.info(
        "SRE triage: provider breach incident %s (%s, provider=%s, count=%d, backend=%s)",
        action, incident.get("incident_id", "?"), provider, failure_count, backend,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def _triage_loop(shutdown: asyncio.Event) -> None:
    """Background loop that runs SRE triage checks every 5 minutes."""
    logger.info("SRE triage: background task started (interval=%ds)", _INTERVAL_SECONDS)

    while not shutdown.is_set():
        try:
            await _run_triage_cycle()
        except Exception as e:
            logger.error("SRE triage: cycle failed unexpectedly: %s", e, exc_info=True)

        # Wait for interval or shutdown, whichever comes first
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=_INTERVAL_SECONDS)
            break  # shutdown was signaled
        except asyncio.TimeoutError:
            pass  # interval elapsed, run next cycle

    logger.info("SRE triage: background task stopped")


async def _run_triage_cycle() -> None:
    """Execute one triage cycle (best-effort, non-fatal)."""
    client = _get_supabase()
    if client is None:
        logger.debug("SRE triage: no Supabase client available, skipping cycle")
        return

    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=_INTERVAL_SECONDS)
    since_iso = since.isoformat()

    # --- Check 1: Receipt failure spike ---
    failure_count = await asyncio.to_thread(_query_receipt_failures, client, since_iso)
    if failure_count >= _RECEIPT_FAILURE_THRESHOLD:
        logger.warning(
            "SRE triage: receipt failure spike detected (%d failures in 5 min)",
            failure_count,
        )
        await asyncio.to_thread(_create_receipt_breach_incident, failure_count)
    else:
        logger.debug("SRE triage: receipts OK (%d failures in window)", failure_count)

    # --- Check 2: Provider failure spikes ---
    provider_counts = await asyncio.to_thread(_query_provider_failures, client, since_iso)
    for provider, count in provider_counts.items():
        if count >= _PROVIDER_FAILURE_THRESHOLD:
            logger.warning(
                "SRE triage: provider failure spike — %s has %d failures in 5 min",
                provider, count,
            )
            await asyncio.to_thread(_create_provider_breach_incident, provider, count)
        else:
            logger.debug("SRE triage: provider %s OK (%d failures)", provider, count)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_sre_triage() -> None:
    """Start the SRE triage background task. Call from server startup.

    Safe to call multiple times — subsequent calls are no-ops.
    Non-blocking: creates an asyncio task on the running event loop.
    """
    global _triage_task, _shutdown_event

    if _triage_task is not None and not _triage_task.done():
        logger.debug("SRE triage: already running, skipping start")
        return

    _shutdown_event = asyncio.Event()
    _triage_task = asyncio.create_task(
        _triage_loop(_shutdown_event),
        name="sre-triage-background",
    )
    logger.info("SRE triage: background task scheduled")


async def stop_sre_triage() -> None:
    """Stop the SRE triage background task gracefully. Call from server shutdown."""
    global _triage_task, _shutdown_event

    if _shutdown_event is not None:
        _shutdown_event.set()

    if _triage_task is not None and not _triage_task.done():
        try:
            await asyncio.wait_for(_triage_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("SRE triage: task did not stop within 10s, cancelling")
            _triage_task.cancel()
            try:
                await _triage_task
            except asyncio.CancelledError:
                pass

    _triage_task = None
    _shutdown_event = None
    logger.info("SRE triage: shutdown complete")
