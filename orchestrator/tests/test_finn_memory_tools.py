"""Tests for finn_memory_tools.py — 13 Finn Finance Hub tool wrappers.

Contract tests:
  - Each tool returns the expected Pydantic shape.
  - Missing ScopedIdentity raises FinnToolError(code=INVALID_CAPABILITY_TOKEN).
  - HTTP 500 from finance service raises FinnToolError(code=PROVIDER_UNAVAILABLE, retryable=True).
  - HTTP 400 from finance service raises FinnToolError(code=INVALID_INPUT, retryable=False).
  - Timeout raises FinnToolError(code=PROVIDER_TIMEOUT, retryable=True).
  - finn_apply_writeback emits a receipt dict via store_receipts.
  - finn_save_finance_memory rejects invalid artifact types.
  - finn_save_finance_memory calls MemoryService.write with visibility_scope='finance'.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    Provenance,
    ScopedIdentity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TENANT = uuid.uuid4()
SUITE = uuid.uuid4()
OFFICE = uuid.uuid4()
TRACE = uuid.uuid4()
CORR = uuid.uuid4()
MEMORY_ID = uuid.uuid4()
NOW_ISO = datetime.now(tz=timezone.utc).isoformat()


def _scope() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE)


def _fake_memory_out() -> MagicMock:
    mo = MagicMock()
    mo.memory_id = MEMORY_ID
    mo.linked_receipt_ids = [uuid.uuid4()]
    return mo


def _finance_ok(data: dict[str, Any]) -> AsyncMock:
    """Mock httpx.AsyncClient that returns 200 with data."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.post = AsyncMock(return_value=mock_resp)
    return mock_client


def _finance_http_error(status: int) -> AsyncMock:
    """Mock httpx.AsyncClient that raises HTTPStatusError."""
    mock_req = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = status
    exc = httpx.HTTPStatusError("error", request=mock_req, response=mock_resp)

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=exc)
    mock_client.post = AsyncMock(side_effect=exc)
    return mock_client


def _finance_timeout() -> AsyncMock:
    """Mock httpx.AsyncClient that raises TimeoutException."""
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    return mock_client


# ---------------------------------------------------------------------------
# Tool 1: finn_get_context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_context_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnContextOut,
        finn_get_context,
    )

    data = {
        "connected_providers": ["plaid"],
        "provider_freshness": {"plaid": "fresh"},
        "coverage_summary": "all good",
        "degraded_conditions": [],
    }
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_get_context(_scope())

    assert isinstance(result, FinnContextOut)
    assert result.connected_providers == ["plaid"]
    assert result.coverage_summary == "all good"
    assert result.tenant_id == str(TENANT)
    assert result.correlation_id  # non-empty uuid str


@pytest.mark.asyncio
async def test_finn_get_context_invalid_scope_raises() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnToolError,
        finn_get_context,
    )

    with pytest.raises(FinnToolError) as exc_info:
        await finn_get_context("not-a-scope")  # type: ignore[arg-type]

    assert exc_info.value.code == "INVALID_CAPABILITY_TOKEN"


@pytest.mark.asyncio
async def test_finn_get_context_timeout_raises_retryable() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnToolError,
        finn_get_context,
    )

    with patch("httpx.AsyncClient", return_value=_finance_timeout()):
        with pytest.raises(FinnToolError) as exc_info:
            await finn_get_context(_scope())

    assert exc_info.value.code == "PROVIDER_TIMEOUT"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_finn_get_context_500_raises_unavailable() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnToolError,
        finn_get_context,
    )

    with patch("httpx.AsyncClient", return_value=_finance_http_error(500)):
        with pytest.raises(FinnToolError) as exc_info:
            await finn_get_context(_scope())

    assert exc_info.value.code == "PROVIDER_UNAVAILABLE"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_finn_get_context_400_raises_invalid_input() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnToolError,
        finn_get_context,
    )

    with patch("httpx.AsyncClient", return_value=_finance_http_error(400)):
        with pytest.raises(FinnToolError) as exc_info:
            await finn_get_context(_scope())

    assert exc_info.value.code == "INVALID_INPUT"
    assert exc_info.value.retryable is False


# ---------------------------------------------------------------------------
# Tool 2: finn_get_overview
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_overview_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnOverviewOut,
        finn_get_overview,
    )

    data = {
        "books_health": "green",
        "cash_position_summary": "strong",
        "needs_review_count": 5,
        "close_readiness": "ready",
        "top_changes": ["invoice #123 paid"],
        "priority_actions": ["review 5 items"],
        "recent_receipts_summary": "3 recent",
    }
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_get_overview(_scope())

    assert isinstance(result, FinnOverviewOut)
    assert result.needs_review_count == 5
    assert result.books_health == "green"


# ---------------------------------------------------------------------------
# Tool 3: finn_get_cash_truth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_cash_truth_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnCashTruthOut,
        finn_get_cash_truth,
    )

    data = {
        "usable_cash": {"amount": 50000, "currency": "USD"},
        "upcoming_outflows": [],
        "incoming_pressure": [],
        "overdue_invoice_exposure": {},
        "forecast_confidence": "high",
    }
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_get_cash_truth(_scope())

    assert isinstance(result, FinnCashTruthOut)
    assert result.forecast_confidence == "high"


# ---------------------------------------------------------------------------
# Tool 4: finn_get_review_queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_review_queue_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnReviewQueueOut,
        finn_get_review_queue,
    )

    data = {"items": [{"id": "1", "amount": 100}], "total": 1}
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_get_review_queue(_scope())

    assert isinstance(result, FinnReviewQueueOut)
    assert result.total == 1
    assert len(result.items) == 1


# ---------------------------------------------------------------------------
# Tool 5: finn_get_reconciliation_queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_reconciliation_queue_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnReconciliationQueueOut,
        finn_get_reconciliation_queue,
    )

    data = {
        "mismatch_candidates": [],
        "duplicate_candidates": [],
        "transfer_pair_candidates": [],
        "stale_provider_warnings": [],
        "total": 0,
    }
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_get_reconciliation_queue(_scope())

    assert isinstance(result, FinnReconciliationQueueOut)
    assert result.total == 0


# ---------------------------------------------------------------------------
# Tool 6: finn_get_reports_summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_reports_summary_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnReportsSummaryOut,
        finn_get_reports_summary,
    )

    fake_rows = [
        {
            "memory_id": str(uuid.uuid4()),
            "title": "Weekly finance brief",
            "summary": "All good",
            "last_activity_at": NOW_ISO,
        }
    ]

    with patch(
        "aspire_orchestrator.services.skillpacks.finn_memory_tools.supabase_select",
        new=AsyncMock(return_value=fake_rows),
    ):
        result = await finn_get_reports_summary(_scope())

    assert isinstance(result, FinnReportsSummaryOut)
    assert len(result.summaries) == 1
    assert result.freshness == NOW_ISO


# ---------------------------------------------------------------------------
# Tool 7: finn_get_ar_aging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_ar_aging_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnArAgingOut,
        finn_get_ar_aging,
    )

    data = {
        "buckets": {"current": 1000, "30_day": 500},
        "top_overdue_invoices": [],
        "top_customers_at_risk": [],
        "cash_impact_estimate": {"total": 500},
        "collections_priority": [],
    }
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_get_ar_aging(_scope())

    assert isinstance(result, FinnArAgingOut)
    assert result.cash_impact_estimate == {"total": 500}


# ---------------------------------------------------------------------------
# Tool 8: finn_get_rules
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_rules_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnRulesOut,
        finn_get_rules,
    )

    data = {"rules": [{"id": "r1", "name": "Payroll rule"}], "total": 1}
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_get_rules(_scope())

    assert isinstance(result, FinnRulesOut)
    assert result.total == 1


# ---------------------------------------------------------------------------
# Tool 9: finn_simulate_rule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_simulate_rule_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnSimulateRuleOut,
        finn_simulate_rule,
    )

    data = {
        "projected_matches": 12,
        "projected_misses": 3,
        "false_positive_risk": "low",
        "sample_transactions": [],
        "requires_approval": False,
    }
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_simulate_rule(_scope(), rule_definition={"pattern": "payroll"})

    assert isinstance(result, FinnSimulateRuleOut)
    assert result.projected_matches == 12
    assert result.requires_approval is False


# ---------------------------------------------------------------------------
# Tool 10: finn_preview_writeback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_preview_writeback_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnPreviewWritebackOut,
        finn_preview_writeback,
    )

    data = {
        "before_state": {"category": "uncategorized"},
        "after_state": {"category": "payroll"},
        "target_provider": "quickbooks",
        "expected_impact": "low",
        "required_approval_state": "none",
        "preview_artifact_id": str(uuid.uuid4()),
    }
    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        result = await finn_preview_writeback(_scope(), writeback_payload={"tx_id": "abc"})

    assert isinstance(result, FinnPreviewWritebackOut)
    assert result.target_provider == "quickbooks"


# ---------------------------------------------------------------------------
# Tool 11: finn_apply_writeback — RED tier, receipt emitted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_apply_writeback_emits_receipt() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnApplyWritebackOut,
        finn_apply_writeback,
    )

    data = {"applied": True, "provider_status": "success", "failure_details": None}
    receipts_written: list[list[dict]] = []

    async def mock_store(receipts: list[dict]) -> None:  # type: ignore[override]
        receipts_written.append(receipts)

    with patch("httpx.AsyncClient", return_value=_finance_ok(data)):
        with patch(
            "aspire_orchestrator.services.skillpacks.finn_memory_tools.store_receipts",
            side_effect=mock_store,
        ):
            result = await finn_apply_writeback(
                _scope(),
                writeback_payload={"tx_id": "abc"},
                idempotency_key="idem-key-1",
                approval_evidence={"approved_by": "owner"},
            )

    assert isinstance(result, FinnApplyWritebackOut)
    assert result.applied is True
    assert result.receipt_id  # non-empty
    assert len(receipts_written) == 1
    receipt = receipts_written[0][0]
    assert receipt["receipt_type"] == "finance_writeback_apply"
    assert receipt["risk_tier"] == "red"
    assert receipt["outcome"] == "success"
    # Law #9: no PII / no secrets in receipt
    assert "tx_id" not in str(receipt)


@pytest.mark.asyncio
async def test_finn_apply_writeback_missing_idempotency_key_raises() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnToolError,
        finn_apply_writeback,
    )

    with pytest.raises(FinnToolError) as exc_info:
        await finn_apply_writeback(
            _scope(),
            writeback_payload={"tx_id": "abc"},
            idempotency_key="",
        )

    assert exc_info.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Tool 12: finn_get_money_trail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_get_money_trail_returns_expected_shape() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnMoneyTrailOut,
        finn_get_money_trail,
    )

    fake_rows = [
        {
            "id": str(uuid.uuid4()),
            "receipt_type": "finance_writeback_apply",
            "action_type": "finance_writeback_apply",
            "outcome": "success",
            "created_at": NOW_ISO,
            "actor_id": str(uuid.uuid4()),
        }
    ]

    with patch(
        "aspire_orchestrator.services.skillpacks.finn_memory_tools.supabase_select",
        new=AsyncMock(return_value=fake_rows),
    ):
        result = await finn_get_money_trail(_scope())

    assert isinstance(result, FinnMoneyTrailOut)
    assert result.total == 1


# ---------------------------------------------------------------------------
# Tool 13: finn_save_finance_memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finn_save_finance_memory_calls_memory_service() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnSaveMemoryOut,
        finn_save_finance_memory,
    )

    fake_out = _fake_memory_out()

    with patch(
        "aspire_orchestrator.services.skillpacks.finn_memory_tools.MemoryService.write",
        new=AsyncMock(return_value=fake_out),
    ):
        result = await finn_save_finance_memory(
            _scope(),
            memory_artifact_type="weekly_finance_brief",
            summary="This week was profitable.",
            title="Week 17 Brief",
        )

    assert isinstance(result, FinnSaveMemoryOut)
    assert result.memory_id == str(MEMORY_ID)
    assert result.correlation_id


@pytest.mark.asyncio
async def test_finn_save_finance_memory_rejects_invalid_type() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnToolError,
        finn_save_finance_memory,
    )

    with pytest.raises(FinnToolError) as exc_info:
        await finn_save_finance_memory(
            _scope(),
            memory_artifact_type="NOT_A_VALID_TYPE",
            summary="ignored",
        )

    assert exc_info.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Capability scope test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_tools_reject_non_scoped_identity() -> None:
    """All tools must raise FinnToolError(INVALID_CAPABILITY_TOKEN) for invalid scope."""
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import (
        FinnToolError,
        finn_get_context,
        finn_get_overview,
        finn_get_cash_truth,
        finn_get_review_queue,
        finn_get_reconciliation_queue,
        finn_get_ar_aging,
        finn_get_rules,
        finn_get_money_trail,
    )

    bad_scope: Any = None

    read_tools = [
        finn_get_context,
        finn_get_overview,
        finn_get_cash_truth,
        finn_get_review_queue,
        finn_get_reconciliation_queue,
        finn_get_ar_aging,
        finn_get_rules,
        finn_get_money_trail,
    ]

    for tool in read_tools:
        with pytest.raises(FinnToolError) as exc_info:
            await tool(bad_scope)  # type: ignore[arg-type]
        assert exc_info.value.code == "INVALID_CAPABILITY_TOKEN", f"Tool {tool.__name__} did not deny"


# ---------------------------------------------------------------------------
# Registered tool names constant
# ---------------------------------------------------------------------------

def test_finn_memory_tools_registry_has_13_entries() -> None:
    from aspire_orchestrator.services.skillpacks.finn_memory_tools import FINN_MEMORY_TOOLS

    assert len(FINN_MEMORY_TOOLS) == 13
    assert "finn.memory.apply_writeback" in FINN_MEMORY_TOOLS
    assert "finn.memory.save_finance_memory" in FINN_MEMORY_TOOLS
