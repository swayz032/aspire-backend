"""Wave 20.14.5L L-3 — Replay determinism helpers.

Two hash levels:
  - edge-level replay_hash (in taxonomy.compute_replay_hash) — per-edge
  - run-level replay_hash (here) — per-generator-invocation

Run-level hash binds generator identity, taxonomy version, project, and
ordered candidate inputs so that identical reruns are detectable + the
output_edge_hash of the receipt is meaningful.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .taxonomy import EDGE_CONTRACTS, EdgeType


def compute_taxonomy_version() -> str:
    """Deterministic version string over the current EDGE_CONTRACTS registry.

    Re-computed at import time of this module. Changes only when a new
    edge type is added, a contract field is altered, or a confidence
    threshold moves. Used to invalidate replay receipts when the
    taxonomy itself shifts.
    """
    canonical: list[dict[str, Any]] = []
    for et in EdgeType:
        c = EDGE_CONTRACTS[et]
        canonical.append({
            "edge_type": et.value,
            "edge_class": c.edge_class,
            "valid_src_types": sorted(t.value for t in c.valid_src_types),
            "valid_dst_types": sorted(t.value for t in c.valid_dst_types),
            "valid_locality_scopes": sorted(s.value for s in c.valid_locality_scopes),
            "spatial_distance_required": c.spatial_distance_required,
            "symmetric": c.symmetric,
            "directional": c.directional,
            "required_evidence_keys": sorted(c.required_evidence_keys),
            "required_provenance_keys": sorted(c.required_provenance_keys),
            "confidence_floor": c.confidence_floor,
            "ambiguity_floor": c.ambiguity_floor,
        })
    encoded = json.dumps(canonical, sort_keys=True).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:16]


# Computed once at import time
TAXONOMY_VERSION: str = compute_taxonomy_version()


def compute_input_evidence_hash(
    *,
    generator_name: str,
    generator_version: str,
    project_id: str,
    sheet_ids: Iterable[str] = (),
    extra_inputs: dict[str, Any] | None = None,
) -> str:
    """Deterministic hash of the run's inputs.

    Sheet IDs are sorted before hashing — DB iteration order must not
    affect the hash.
    """
    canonical = json.dumps(
        {
            "generator_name": generator_name,
            "generator_version": generator_version,
            "taxonomy_version": TAXONOMY_VERSION,
            "project_id": str(project_id),
            "sheet_ids": sorted(str(s) for s in sheet_ids),
            "extra_inputs": extra_inputs or {},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:24]


def compute_output_edge_hash(replay_hashes: Iterable[str]) -> str:
    """Deterministic hash over the set of emitted edge replay_hashes.

    Sorted before hashing. Two reruns that emit the same edge set
    produce the same output_edge_hash, regardless of the order edges
    were discovered.
    """
    sorted_hashes = sorted(replay_hashes)
    canonical = "|".join(sorted_hashes)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:24]


def stable_candidate_sort_key(candidate: Any) -> tuple:
    """Stable ordering key for candidate iteration.

    Reviewer requirement: never trust DB return order, Python dict order,
    or concurrent iteration timing. All generators must sort candidates
    by this key before evaluation.

    Expects candidates to expose: project_id, sheet_id, src_entity_id,
    dst_entity_id, edge_type (as strings/uuids).
    """
    return (
        str(getattr(candidate, "project_id", "")),
        str(getattr(candidate, "sheet_id", "") or ""),
        str(getattr(candidate, "src_entity_id", "")),
        str(getattr(candidate, "dst_entity_id", "")),
        str(getattr(candidate, "edge_type", "")),
    )
