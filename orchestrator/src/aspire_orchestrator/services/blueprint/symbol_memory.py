"""Wave 11i — Tenant Symbol Memory helpers (per-tenant data flywheel moat).

Reads/writes blueprint_symbol_memory (per-tenant, deterministic signature →
canonical_name mapping). All operations RLS-scoped by suite_id (Law #6).

Three operations:
  - lookup_by_signature(suite_id, signature_hash) → canonical_name | None
  - bulk_lookup(suite_id, list[signature_hash]) → {sig: name}
  - upsert(suite_id, signature_hash, canonical_name, ...) → on confirmation

The moat: when a contractor uploads project #2 from the same architect, every
signature already in their memory resolves INSTANTLY. No model retraining.
No labels needed. The contractor's first project + corrections taught us
their entire vocabulary deterministically.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)

_TABLE = "blueprint_symbol_memory"


async def lookup_by_signature(suite_id: str, signature_hash: str) -> dict | None:
    """Exact-hash lookup. Returns the row dict or None."""
    try:
        rows = await supabase_select(
            _TABLE,
            filters=f"suite_id=eq.{suite_id}&signature_hash=eq.{signature_hash}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("symbol_memory: lookup failed (%s) sig=%s", type(exc).__name__, signature_hash[:8])
        return None
    if not rows:
        return None
    return rows[0]


async def bulk_lookup(suite_id: str, signature_hashes: list[str]) -> dict[str, dict]:
    """Bulk lookup. Returns {signature_hash: row_dict} for hits."""
    if not signature_hashes:
        return {}
    # PostgREST `in.()` filter for batch fetch
    sigs = ",".join(set(signature_hashes))
    try:
        rows = await supabase_select(
            _TABLE,
            filters=f"suite_id=eq.{suite_id}&signature_hash=in.({sigs})",
            limit=len(signature_hashes) + 10,
        )
    except SupabaseClientError as exc:
        logger.warning("symbol_memory: bulk lookup failed (%s)", type(exc).__name__)
        return {}
    return {r["signature_hash"]: r for r in (rows or [])}


async def upsert_observation(
    *,
    suite_id: str,
    office_id: str | None,
    signature_hash: str,
    canonical_name: str,
    category: str | None,
    source: str,
    project_id: str | None,
    instances_observed: int,
    confidence: float | None = None,
    is_confirmation: bool = False,
) -> dict | None:
    """Upsert a row. On hit, bump counters; on miss, insert.

    Args:
        source: 'legend' | 'confirmation' | 'vlm' | 'manual' | 'tenant_memory'
        is_confirmation: True when a contractor explicitly confirmed the name.
            Increments confirmation_count (used by Wave 11j retrain trigger).
    """
    existing = await lookup_by_signature(suite_id, signature_hash)
    now_iso = datetime.now(timezone.utc).isoformat()

    if existing is None:
        # Insert fresh row
        try:
            return await supabase_insert(_TABLE, {
                "suite_id": suite_id,
                "office_id": office_id,
                "signature_hash": signature_hash,
                "canonical_name": canonical_name,
                "category": category,
                "source": source,
                "first_seen_project_id": project_id,
                "last_seen_project_id": project_id,
                "confirmation_count": 1 if is_confirmation else 0,
                "total_instances_observed": instances_observed,
                "confidence": confidence,
                "created_at": now_iso,
                "updated_at": now_iso,
            })
        except SupabaseClientError as exc:
            logger.warning("symbol_memory: insert failed (%s) sig=%s", type(exc).__name__, signature_hash[:8])
            return None

    # Update existing — never overwrite a confirmation-sourced name with a
    # less-authoritative source (legend < confirmation; vlm < legend; unanchored < anything).
    source_priority = {"manual": 5, "confirmation": 4, "legend": 3, "tenant_memory": 2, "vlm": 1, "unanchored": 0}
    new_priority = source_priority.get(source, 0)
    existing_priority = source_priority.get(existing.get("source", "unanchored"), 0)
    update_payload: dict[str, Any] = {
        "last_seen_project_id": project_id,
        "total_instances_observed": (existing.get("total_instances_observed") or 0) + instances_observed,
        "updated_at": now_iso,
    }
    if is_confirmation:
        update_payload["confirmation_count"] = (existing.get("confirmation_count") or 0) + 1
    if new_priority > existing_priority and canonical_name:
        update_payload["canonical_name"] = canonical_name
        update_payload["category"] = category or existing.get("category")
        update_payload["source"] = source
        update_payload["confidence"] = confidence
    try:
        await supabase_update(
            _TABLE,
            f"id=eq.{existing['id']}&suite_id=eq.{suite_id}",
            update_payload,
        )
    except SupabaseClientError as exc:
        logger.warning("symbol_memory: update failed (%s) sig=%s", type(exc).__name__, signature_hash[:8])
        return existing
    return {**existing, **update_payload}


async def resolve_motif_names(
    *,
    suite_id: str,
    motifs: list,  # list[MotifDetection]
    legend_map: dict[str, str],
) -> list:
    """For each motif, set canonical_name + name_source via:
       1. tenant memory (highest authority)
       2. page legend
       3. fall back to 'unknown_motif_<sig8>' with name_source='unanchored'

    Returns a new list of MotifDetection-like dicts with name fields set.
    """
    # Avoid circular import: import the dataclass lazily
    from aspire_orchestrator.services.blueprint.vgse import MotifDetection

    if not motifs:
        return []
    sigs = [m.signature_hash for m in motifs]
    memory_hits = await bulk_lookup(suite_id, sigs)
    resolved: list[MotifDetection] = []
    for m in motifs:
        mem = memory_hits.get(m.signature_hash)
        if mem and mem.get("canonical_name"):
            resolved.append(MotifDetection(
                signature_hash=m.signature_hash,
                count=m.count,
                sample_item_count=m.sample_item_count,
                sample_size_pt=m.sample_size_pt,
                spatial_entropy=m.spatial_entropy,
                bboxes=m.bboxes,
                canonical_name=mem["canonical_name"],
                name_source="tenant_memory",
            ))
        elif m.signature_hash in legend_map:
            resolved.append(MotifDetection(
                signature_hash=m.signature_hash,
                count=m.count,
                sample_item_count=m.sample_item_count,
                sample_size_pt=m.sample_size_pt,
                spatial_entropy=m.spatial_entropy,
                bboxes=m.bboxes,
                canonical_name=legend_map[m.signature_hash],
                name_source="legend",
            ))
        else:
            resolved.append(MotifDetection(
                signature_hash=m.signature_hash,
                count=m.count,
                sample_item_count=m.sample_item_count,
                sample_size_pt=m.sample_size_pt,
                spatial_entropy=m.spatial_entropy,
                bboxes=m.bboxes,
                canonical_name=f"unknown_motif_{m.signature_hash[:8]}",
                name_source="unanchored",
            ))
    return resolved
