"""Tests for Stage 6b — vlm_symbol_verifier.verify_borderline_binding().

Five required tests (per plan v7 §2.7.6):
1. Happy path: mocked YES response → binds cluster, emits receipt
2. NO response: leaves cluster unknown, emits receipt
3. Cap exhausted: subsequent calls skip + emit denial receipt
4. Network failure: emits error receipt, leaves cluster unknown (Law #3 fail-closed)
5. Cross-tenant: providing suite_id A returns empty when project belongs to suite B (Law #6)
"""
from __future__ import annotations

import io
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from aspire_orchestrator.services.blueprint.vlm_symbol_verifier import (
    _parse_verdict,
    verify_borderline_binding,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SUITE_A = "aaaaaaaa-0000-0000-0000-000000000001"
_SUITE_B = "bbbbbbbb-0000-0000-0000-000000000002"
_PROJECT_ID = "cccccccc-0000-0000-0000-000000000003"
_CLUSTER_ID = str(uuid.uuid4())
_LEGEND_ID = str(uuid.uuid4())
_SHEET_ID = str(uuid.uuid4())
_CORR_ID = str(uuid.uuid4())

_MOTIF_BBOX = {"x0": 100, "y0": 200, "x1": 150, "y1": 250}


def _tiny_png() -> bytes:
    """Return minimal valid PNG bytes (10×10 white image)."""
    img = Image.new("RGB", (10, 10), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mock_project_row() -> list[dict[str, Any]]:
    return [{"id": _PROJECT_ID, "suite_id": _SUITE_A}]


def _mock_legend_row() -> list[dict[str, Any]]:
    return [{
        "id": _LEGEND_ID,
        "suite_id": _SUITE_A,
        "source_sheet_id": _SHEET_ID,
        "representation_bbox": {"x0": 50, "y0": 60, "x1": 100, "y1": 110},
    }]


def _openai_result(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "json": {},
        "usage": {"prompt_tokens": 1100, "completion_tokens": 30},
    }


# ---------------------------------------------------------------------------
# Test 1 — Happy path: YES response → verified=True, receipt emitted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_yes_response_binds_cluster() -> None:
    """VLM returns YES → verified=True, receipt emitted with outcome=success."""
    receipts_emitted: list[dict] = []

    def _capture_receipts(r: list[dict]) -> None:
        receipts_emitted.extend(r)

    with (
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.supabase_select",
            new_callable=AsyncMock,
            side_effect=[_mock_project_row(), _mock_legend_row()],
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier._fetch_cropped_motif",
            new_callable=AsyncMock,
            return_value=_tiny_png(),
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier._fetch_cropped_legend",
            new_callable=AsyncMock,
            return_value=_tiny_png(),
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.store_receipts",
            side_effect=_capture_receipts,
        ),
        patch(
            "aspire_orchestrator.services.openai_client.generate_json_async",
            new_callable=AsyncMock,
            return_value=_openai_result("YES The circular glyphs match in geometry and context."),
        ),
        patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
    ):
        spend_tracker: dict[str, float] = {}
        result = await verify_borderline_binding(
            suite_id=_SUITE_A,
            project_id=_PROJECT_ID,
            cluster_id=_CLUSTER_ID,
            motif_bbox=_MOTIF_BBOX,
            motif_sheet_id=_SHEET_ID,
            candidate_legend_id=_LEGEND_ID,
            candidate_legend_label="QR Pendant V2708",
            correlation_id=_CORR_ID,
            _project_spend_tracker=spend_tracker,
        )

    assert result["verified"] is True, f"Expected verified=True; got {result}"
    assert result["verdict"] == "yes"
    assert result["cost_usd"] > 0.0

    # Receipt must be emitted with outcome=success
    assert receipts_emitted, "Expected at least one receipt"
    receipt = receipts_emitted[-1]
    assert receipt["outcome"] == "success"
    assert receipt["action_type"] == "provider.openai.vlm_verifier"
    assert receipt["metadata"]["verdict"] == "yes"


# ---------------------------------------------------------------------------
# Test 2 — NO response: cluster stays unknown, receipt emitted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_response_leaves_cluster_unknown() -> None:
    """VLM returns NO → verified=False, receipt emitted with outcome=failure."""
    receipts_emitted: list[dict] = []

    with (
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.supabase_select",
            new_callable=AsyncMock,
            side_effect=[_mock_project_row(), _mock_legend_row()],
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier._fetch_cropped_motif",
            new_callable=AsyncMock,
            return_value=_tiny_png(),
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier._fetch_cropped_legend",
            new_callable=AsyncMock,
            return_value=_tiny_png(),
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.store_receipts",
            side_effect=lambda r: receipts_emitted.extend(r),
        ),
        patch(
            "aspire_orchestrator.services.openai_client.generate_json_async",
            new_callable=AsyncMock,
            return_value=_openai_result("NO The motif lacks the diagonal hash marks of the legend."),
        ),
        patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
    ):
        result = await verify_borderline_binding(
            suite_id=_SUITE_A,
            project_id=_PROJECT_ID,
            cluster_id=_CLUSTER_ID,
            motif_bbox=_MOTIF_BBOX,
            motif_sheet_id=_SHEET_ID,
            candidate_legend_id=_LEGEND_ID,
            candidate_legend_label="Fire Sprinkler Head",
            correlation_id=_CORR_ID,
        )

    assert result["verified"] is False
    assert result["verdict"] == "no"

    # Receipt must be emitted with outcome=failure (not success)
    assert receipts_emitted
    receipt = receipts_emitted[-1]
    assert receipt["outcome"] == "failure"
    assert receipt["metadata"]["verdict"] == "no"


# ---------------------------------------------------------------------------
# Test 3 — Cap exhausted: calls skip, denial receipt emitted per call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cap_exhausted_skips_and_emits_denial_receipt() -> None:
    """When project spend ≥ cap, call returns cap_denied without hitting OpenAI."""
    receipts_emitted: list[dict] = []

    with (
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.supabase_select",
            new_callable=AsyncMock,
            return_value=_mock_project_row(),
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.store_receipts",
            side_effect=lambda r: receipts_emitted.extend(r),
        ),
        patch.dict("os.environ", {
            "OPENAI_API_KEY": "test-key",
            "ASPIRE_VLM_VERIFIER_CAP_USD": "0.01",
        }),
    ):
        # Spend tracker already at cap
        spend_tracker = {_PROJECT_ID: 0.01}

        result = await verify_borderline_binding(
            suite_id=_SUITE_A,
            project_id=_PROJECT_ID,
            cluster_id=_CLUSTER_ID,
            motif_bbox=_MOTIF_BBOX,
            motif_sheet_id=_SHEET_ID,
            candidate_legend_id=_LEGEND_ID,
            candidate_legend_label="Electrical Junction",
            correlation_id=_CORR_ID,
            _project_spend_tracker=spend_tracker,
        )

    assert result["verified"] is False
    assert result["verdict"] == "cap_denied"
    assert result["cost_usd"] == 0.0

    # Denial receipt must be emitted
    assert receipts_emitted
    receipt = receipts_emitted[-1]
    assert receipt["outcome"] == "denied"
    assert "cap" in receipt["metadata"]["reason"]


# ---------------------------------------------------------------------------
# Test 4 — Network failure: error receipt, cluster unknown (Law #3 fail-closed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_network_failure_emits_error_receipt_and_leaves_cluster_unknown() -> None:
    """OpenAI call raises → verified=False, error receipt emitted, no crash."""
    receipts_emitted: list[dict] = []

    with (
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.supabase_select",
            new_callable=AsyncMock,
            side_effect=[_mock_project_row(), _mock_legend_row()],
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier._fetch_cropped_motif",
            new_callable=AsyncMock,
            return_value=_tiny_png(),
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier._fetch_cropped_legend",
            new_callable=AsyncMock,
            return_value=_tiny_png(),
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.store_receipts",
            side_effect=lambda r: receipts_emitted.extend(r),
        ),
        patch(
            "aspire_orchestrator.services.openai_client.generate_json_async",
            new_callable=AsyncMock,
            side_effect=ConnectionError("Network unreachable"),
        ),
        patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
    ):
        result = await verify_borderline_binding(
            suite_id=_SUITE_A,
            project_id=_PROJECT_ID,
            cluster_id=_CLUSTER_ID,
            motif_bbox=_MOTIF_BBOX,
            motif_sheet_id=_SHEET_ID,
            candidate_legend_id=_LEGEND_ID,
            candidate_legend_label="Drain Symbol",
            correlation_id=_CORR_ID,
        )

    # Must not raise — Law #3: fail closed
    assert result["verified"] is False
    assert result["verdict"] == "error"
    assert result["cost_usd"] == 0.0

    # Error receipt must be emitted
    assert receipts_emitted
    receipt = receipts_emitted[-1]
    assert receipt["outcome"] == "failure"
    assert "openai_call_failed" in receipt["metadata"]["reason"]


# ---------------------------------------------------------------------------
# Test 5 — Cross-tenant: suite A call returns empty when project belongs to suite B
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_tenant_request_denied_by_suite_isolation() -> None:
    """Requesting suite_id=A for a project that belongs to suite_id=B → denied.

    supabase_select returns [] (no rows match suite_id=A for this project).
    The verifier must deny without reading any image or calling OpenAI.
    Law #6 enforcement.
    """
    receipts_emitted: list[dict] = []
    openai_called = False

    async def _mock_select(table: str, filters: str, **kwargs: Any) -> list[dict]:
        if "blueprint_projects" in table:
            # Suite B project is not visible to suite A caller
            return []
        return []

    with (
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.supabase_select",
            new_callable=AsyncMock,
            side_effect=_mock_select,
        ),
        patch(
            "aspire_orchestrator.services.blueprint.vlm_symbol_verifier.store_receipts",
            side_effect=lambda r: receipts_emitted.extend(r),
        ),
        patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
    ):
        result = await verify_borderline_binding(
            suite_id=_SUITE_A,         # caller is suite A
            project_id=_PROJECT_ID,    # project actually belongs to suite B
            cluster_id=_CLUSTER_ID,
            motif_bbox=_MOTIF_BBOX,
            motif_sheet_id=_SHEET_ID,
            candidate_legend_id=_LEGEND_ID,
            candidate_legend_label="Plumbing Fixture",
            correlation_id=_CORR_ID,
        )

    assert result["verified"] is False
    assert "isolation" in result["reason"] or "tenant" in result["reason"]

    # A denial receipt must be emitted
    assert receipts_emitted
    receipt = receipts_emitted[-1]
    assert receipt["outcome"] == "denied"
    assert receipt["suite_id"] == _SUITE_A


# ---------------------------------------------------------------------------
# Unit tests for _parse_verdict helper
# ---------------------------------------------------------------------------

def test_parse_verdict_yes() -> None:
    verdict, reason = _parse_verdict("YES They share identical circular topology.")
    assert verdict == "yes"
    assert "circular" in reason


def test_parse_verdict_no() -> None:
    verdict, reason = _parse_verdict("NO The diagonal lines differ significantly.")
    assert verdict == "no"
    assert "diagonal" in reason


def test_parse_verdict_ambiguous_defaults_to_no() -> None:
    """Ambiguous response → no (fail-closed, Law #3)."""
    verdict, reason = _parse_verdict("MAYBE they look similar but I'm unsure.")
    assert verdict == "no"
    assert "ambiguous" in reason
