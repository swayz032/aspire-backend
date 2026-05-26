"""Unit tests for Wave 20.14.5L taxonomy + validator.

Coverage targets per acceptance:
- malformed edge insertion impossible through orchestrator APIs
- every validator rejection produces a structured receipt
- symmetry/directionality contract is enforced
- locality enforced centrally
- replay hash is deterministic
- UNKNOWN_REVIEW operational
- DB CHECK SQL matches the Python enum (drift detection)
"""
from __future__ import annotations

import re

import pytest

from .taxonomy import (
    EDGE_CONTRACTS,
    EdgeContract,
    EdgeProposal,
    EdgeType,
    EntityType,
    LocalityScope,
    RejectionReason,
    ResolutionState,
    compute_replay_hash,
    generate_db_check_sql,
    generate_db_check_sql_entity_type,
    generate_db_check_sql_locality,
    validate_edge,
)


# ---------- helpers ----------

UID_A = "11111111-1111-1111-1111-111111111111"
UID_B = "22222222-2222-2222-2222-222222222222"
UID_SUITE = "00000000-0000-0000-0000-000000000001"
UID_PROJECT = "00000000-0000-0000-0000-000000000002"

_FULL_PROVENANCE = {
    "extractor": "test", "wave": "20.14.5L", "pipeline_version": "1.0",
    "sheet_id": "sheet-uuid", "generator_window": "test", "bbox": [0, 0, 10, 10],
}


def _proposal(edge_type: EdgeType, **overrides) -> EdgeProposal:
    """Build a valid proposal for the given edge type using sane defaults."""
    contract = EDGE_CONTRACTS[edge_type]
    valid_src = next(iter(contract.valid_src_types))
    valid_dst = next(iter(contract.valid_dst_types))
    valid_locality = (
        next(iter(contract.valid_locality_scopes))
        if contract.requires_locality
        else None
    )
    base = dict(
        edge_type=edge_type,
        suite_id=UID_SUITE,
        project_id=UID_PROJECT,
        src_entity_id=UID_A,
        src_entity_type=valid_src,
        dst_entity_id=UID_B,
        dst_entity_type=valid_dst,
        locality_scope=valid_locality,
        confidence=max(contract.confidence_floor, 0.7),
        evidence={k: "x" for k in contract.required_evidence_keys},
        provenance=dict(_FULL_PROVENANCE),
        extraction_method="test_v1",
        spatial_distance=10.0 if contract.spatial_distance_required else None,
    )
    base.update(overrides)
    return EdgeProposal(**base)


# ---------- registry sanity ----------

def test_registry_covers_all_edge_types():
    assert set(EDGE_CONTRACTS.keys()) == set(EdgeType)


def test_every_contract_is_either_symmetric_or_directional():
    for et, contract in EDGE_CONTRACTS.items():
        assert contract.symmetric != contract.directional, (
            f"{et}: must be exactly one of symmetric/directional"
        )


def test_locality_constraint_consistency():
    for et, contract in EDGE_CONTRACTS.items():
        if contract.requires_locality:
            assert contract.valid_locality_scopes, et


def test_confidence_floor_invariant():
    for et, contract in EDGE_CONTRACTS.items():
        assert 0.0 <= contract.ambiguity_floor <= contract.confidence_floor <= 1.0, et


def test_provenance_required_keys_universal():
    expected = {"extractor", "wave", "pipeline_version", "sheet_id",
                "generator_window", "bbox"}
    for et, contract in EDGE_CONTRACTS.items():
        assert expected <= contract.required_provenance_keys, et


# ---------- structural tier ----------

def test_valid_proposal_is_verified():
    result = validate_edge(_proposal(EdgeType.ANNOTATED_BY))
    assert result.resolution_state == ResolutionState.VERIFIED
    assert result.accepted is True


def test_invalid_src_type_rejected():
    p = _proposal(EdgeType.ANNOTATED_BY, src_entity_type=EntityType.SHEET)
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.INVALID_SRC_TYPE in result.final_rejection_reasons
    assert result.structural.passed is False


def test_invalid_dst_type_rejected():
    p = _proposal(EdgeType.ANNOTATED_BY, dst_entity_type=EntityType.SHEET)
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.INVALID_DST_TYPE in result.final_rejection_reasons


def test_self_loop_rejected():
    p = _proposal(EdgeType.ADJACENT_TO, dst_entity_id=UID_A)  # src == dst
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.SELF_LOOP_DISALLOWED in result.final_rejection_reasons


# ---------- semantic tier ----------

def test_missing_locality_rejected():
    p = _proposal(EdgeType.ADJACENT_TO, locality_scope=None)
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.MISSING_LOCALITY in result.final_rejection_reasons


def test_wrong_locality_scope_rejected():
    p = _proposal(EdgeType.ADJACENT_TO, locality_scope=LocalityScope.PROJECT_GLOBAL)
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.INVALID_LOCALITY_SCOPE in result.final_rejection_reasons


def test_missing_evidence_key_rejected():
    p = _proposal(EdgeType.ANNOTATED_BY, evidence={"ocr_text": "x"})  # missing 3 keys
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.EVIDENCE_MISSING_KEYS in result.final_rejection_reasons
    assert result.evidence_missing
    assert "distance_px" in result.evidence_missing


def test_missing_provenance_key_rejected():
    p = _proposal(EdgeType.ANNOTATED_BY, provenance={"extractor": "x"})
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.PROVENANCE_MISSING_KEYS in result.final_rejection_reasons
    assert "sheet_id" in result.provenance_missing


def test_confidence_out_of_range_rejected():
    p = _proposal(EdgeType.ANNOTATED_BY, confidence=1.5)
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.CONFIDENCE_OUT_OF_RANGE in result.final_rejection_reasons


def test_spatial_distance_required_when_contract_demands():
    # ADJACENT_TO requires spatial_distance
    p = _proposal(EdgeType.ADJACENT_TO, spatial_distance=None)
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.SPATIAL_DISTANCE_REQUIRED in result.final_rejection_reasons


# ---------- confidence tri-state (UNKNOWN_REVIEW) ----------

def test_confidence_in_ambiguity_band_routes_to_unknown_review():
    contract = EDGE_CONTRACTS[EdgeType.ANNOTATED_BY]
    mid = (contract.confidence_floor + contract.ambiguity_floor) / 2
    p = _proposal(EdgeType.ANNOTATED_BY, confidence=mid)
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.UNKNOWN_REVIEW
    assert result.review_required is True
    assert RejectionReason.SEMANTIC_AMBIGUITY in result.final_rejection_reasons


def test_confidence_below_ambiguity_floor_rejected():
    contract = EDGE_CONTRACTS[EdgeType.ANNOTATED_BY]
    below = max(0.0, contract.ambiguity_floor - 0.1)
    p = _proposal(EdgeType.ANNOTATED_BY, confidence=below)
    result = validate_edge(p)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.CONFIDENCE_BELOW_FLOOR in result.final_rejection_reasons


# ---------- replay hash determinism ----------

def test_replay_hash_deterministic():
    p1 = _proposal(EdgeType.ANNOTATED_BY)
    p2 = _proposal(EdgeType.ANNOTATED_BY)
    assert compute_replay_hash(p1) == compute_replay_hash(p2)


def test_replay_hash_differs_on_evidence_change():
    p1 = _proposal(EdgeType.ANNOTATED_BY)
    p2 = _proposal(
        EdgeType.ANNOTATED_BY,
        evidence={**p1.evidence, "ocr_text": "different"},
    )
    assert compute_replay_hash(p1) != compute_replay_hash(p2)


def test_replay_hash_in_validation_result():
    p = _proposal(EdgeType.ANNOTATED_BY)
    result = validate_edge(p)
    assert result.replay_hash == compute_replay_hash(p)
    assert re.fullmatch(r"[a-f0-9]{24}", result.replay_hash)


# ---------- constraint tier (Phase 4 hookpoint) ----------

def test_constraint_tier_passes_in_phase3():
    """Constraint tier returns PASS in Phase 3. Phase 4 will inject logic."""
    p = _proposal(EdgeType.ANNOTATED_BY)
    result = validate_edge(p)
    assert result.constraint.passed is True
    assert result.constraint.details.get("phase4_pending") is True


# ---------- DB CHECK SQL drift detector ----------

def test_db_check_sql_matches_enum():
    sql = generate_db_check_sql()
    for et in EdgeType:
        assert f"'{et.value}'" in sql, f"{et} missing from generated CHECK SQL"


def test_db_check_locality_sql_matches_enum():
    sql = generate_db_check_sql_locality()
    for s in LocalityScope:
        assert f"'{s.value}'" in sql


def test_db_check_entity_type_sql_matches_enum():
    sql = generate_db_check_sql_entity_type("src_entity_type")
    for e in EntityType:
        assert f"'{e.value}'" in sql


# ---------- structural sanity across all 20 edge types ----------

@pytest.mark.parametrize("edge_type", list(EdgeType))
def test_default_proposal_validates_for_every_edge_type(edge_type):
    """Every edge type has a valid default proposal that passes validation."""
    result = validate_edge(_proposal(edge_type))
    assert result.resolution_state == ResolutionState.VERIFIED, (
        f"{edge_type}: default proposal failed validation "
        f"with reasons={result.final_rejection_reasons}"
    )


# ---------- symmetric edge contract sanity ----------

def test_symmetric_edges_declared_consistently():
    expected_symmetric = {
        EdgeType.ADJACENT_TO,
        EdgeType.ALIGNED_WITH,
        EdgeType.CONNECTED_TO,
        EdgeType.DISCIPLINE_COMPATIBLE_WITH,
        EdgeType.EXCLUDES,
    }
    for et, contract in EDGE_CONTRACTS.items():
        if et in expected_symmetric:
            assert contract.symmetric, f"{et} should be symmetric"
        else:
            assert contract.directional, f"{et} should be directional"
