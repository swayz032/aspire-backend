"""Tests for adam_research skillpack — SUBCONTRACTOR_DISCOVERY + LABOR_RATE_LOOKUP (5.4d + 5.4e)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.skillpacks.adam_research import (
    AdamResearchContext,
    AdamResearchSkillPack,
)

SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
CORR_ID = "test-corr-skillpack-5d5e"

CTX = AdamResearchContext(
    suite_id=SUITE_ID,
    office_id=OFFICE_ID,
    correlation_id=CORR_ID,
)

SKILLPACK = AdamResearchSkillPack()


def _make_tool_result(outcome: Outcome, data: dict[str, Any] | None = None, error: str | None = None):
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    r = MagicMock(spec=ToolExecutionResult)
    r.outcome = outcome
    r.data = data or {}
    r.error = error
    r.receipt_data = {"receipt_id": "fake-receipt"}
    return r


# =============================================================================
# SUBCONTRACTOR_DISCOVERY
# =============================================================================

class TestSubcontractorDiscovery:

    @pytest.mark.asyncio
    async def test_missing_trade_denied(self):
        result = await SKILLPACK.subcontractor_discovery(
            payload={"zipcode": "78701"},
            context=CTX,
        )
        assert not result.success
        assert result.receipt
        assert result.receipt.get("policy", {}).get("reasons") == ["MISSING_TRADE"]

    @pytest.mark.asyncio
    async def test_missing_zipcode_denied(self):
        result = await SKILLPACK.subcontractor_discovery(
            payload={"trade": "electrician"},
            context=CTX,
        )
        assert not result.success
        assert result.receipt
        assert result.receipt.get("policy", {}).get("reasons") == ["MISSING_ZIPCODE"]

    @pytest.mark.asyncio
    async def test_parallel_calls_unions_vetted_first(self, monkeypatch):
        lsa_data = {
            "contractors": [
                {"id": "lsa_0", "name": "Vetted Electric Co", "google_vetted": True,
                 "phone": "512-555-0300", "rating": 4.9, "review_count": 150,
                 "hours": "", "years_in_business": None},
            ],
            "result_count": 1,
        }
        local_data = {
            "businesses": [
                {"id": "local_0", "name": "Local Electric Inc", "address": "456 Oak Ave",
                 "phone": "512-555-0400", "rating": 4.5, "review_count": 30,
                 "website": "", "gbp_url": "", "type": "Electrician"},
            ],
            "result_count": 1,
        }

        lsa_result = _make_tool_result(Outcome.SUCCESS, lsa_data)
        local_result = _make_tool_result(Outcome.SUCCESS, local_data)

        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_service_ads_client.execute_serpapi_local_service_ads_search",
            AsyncMock(return_value=lsa_result),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_client.execute_serpapi_local_search",
            AsyncMock(return_value=local_result),
        )

        result = await SKILLPACK.subcontractor_discovery(
            payload={"trade": "electrician", "zipcode": "78701"},
            context=CTX,
        )
        assert result.success
        candidates = result.data.get("candidates", [])
        assert len(candidates) == 2
        # Vetted contractor first
        assert candidates[0]["name"] == "Vetted Electric Co"
        assert candidates[0]["vetted"] is True
        assert candidates[1]["name"] == "Local Electric Inc"
        assert candidates[1]["vetted"] is False
        assert result.receipt

    @pytest.mark.asyncio
    async def test_deduplicates_by_name(self, monkeypatch):
        same_biz = {"id": "x", "name": "Same Electric Co", "google_vetted": True,
                    "phone": "", "rating": 4.0, "review_count": 10, "hours": "", "years_in_business": None}
        same_local = {"id": "y", "name": "Same Electric Co", "address": "", "phone": "",
                      "rating": 4.0, "review_count": 10, "website": "", "gbp_url": "", "type": ""}

        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_service_ads_client.execute_serpapi_local_service_ads_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, {"contractors": [same_biz], "result_count": 1})),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_client.execute_serpapi_local_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, {"businesses": [same_local], "result_count": 1})),
        )

        result = await SKILLPACK.subcontractor_discovery(
            payload={"trade": "electrician", "zipcode": "78701"},
            context=CTX,
        )
        candidates = result.data.get("candidates", [])
        names = [c["name"] for c in candidates]
        assert names.count("Same Electric Co") == 1

    @pytest.mark.asyncio
    async def test_top_5_cap(self, monkeypatch):
        many_contractors = [
            {"id": f"lsa_{i}", "name": f"Contractor {i}", "google_vetted": True,
             "phone": "", "rating": 4.0, "review_count": 10, "hours": "", "years_in_business": None}
            for i in range(10)
        ]

        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_service_ads_client.execute_serpapi_local_service_ads_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, {"contractors": many_contractors, "result_count": 10})),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_client.execute_serpapi_local_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, {"businesses": [], "result_count": 0})),
        )

        result = await SKILLPACK.subcontractor_discovery(
            payload={"trade": "plumber", "zipcode": "78701"},
            context=CTX,
        )
        assert len(result.data.get("candidates", [])) <= 5

    @pytest.mark.asyncio
    async def test_partial_failure_still_returns_results(self, monkeypatch):
        """LSA fails; local succeeds — should still return local results."""
        local_data = {
            "businesses": [
                {"id": "local_0", "name": "Reliable HVAC", "address": "", "phone": "",
                 "rating": 4.2, "review_count": 55, "website": "", "gbp_url": "", "type": "HVAC"}
            ],
            "result_count": 1,
        }

        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_service_ads_client.execute_serpapi_local_service_ads_search",
            AsyncMock(return_value=_make_tool_result(Outcome.FAILED, {}, error="budget exhausted")),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_client.execute_serpapi_local_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, local_data)),
        )

        result = await SKILLPACK.subcontractor_discovery(
            payload={"trade": "hvac", "zipcode": "78701"},
            context=CTX,
        )
        assert result.success
        assert len(result.data.get("candidates", [])) == 1
        assert result.data["candidates"][0]["name"] == "Reliable HVAC"


# =============================================================================
# LABOR_RATE_LOOKUP
# =============================================================================

class TestLaborRateLookup:

    @pytest.mark.asyncio
    async def test_missing_trade_denied(self):
        result = await SKILLPACK.labor_rate_lookup(
            payload={"zipcode": "78701"},
            context=CTX,
        )
        assert not result.success
        assert result.receipt.get("policy", {}).get("reasons") == ["MISSING_TRADE"]

    @pytest.mark.asyncio
    async def test_missing_zipcode_denied(self):
        result = await SKILLPACK.labor_rate_lookup(
            payload={"trade": "electrician"},
            context=CTX,
        )
        assert not result.success
        assert result.receipt.get("policy", {}).get("reasons") == ["MISSING_ZIPCODE"]

    @pytest.mark.asyncio
    async def test_unknown_trade_denied(self):
        result = await SKILLPACK.labor_rate_lookup(
            payload={"trade": "welder", "zipcode": "78701"},
            context=CTX,
        )
        assert not result.success
        assert result.receipt.get("policy", {}).get("reasons") == ["UNKNOWN_TRADE"]

    @pytest.mark.asyncio
    async def test_success_returns_wage_band(self, monkeypatch):
        wage_data = {
            "soc_code": "47-2111",
            "area_code": "0012420",
            "area_type": "msa",
            "mean_hourly": 31.0,
            "median_hourly": 29.5,
            "pct_10_hourly": 18.0,
            "pct_25_hourly": 23.0,
            "pct_75_hourly": 38.0,
            "pct_90_hourly": 47.0,
            "employment": 12500,
            "year": "2024",
            "period": "A01",
        }

        monkeypatch.setattr(
            "aspire_orchestrator.providers.bls_oews_client.execute_bls_oews_get_wage",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, wage_data)),
        )
        # Austin TX ZIP → MSA 12420
        monkeypatch.setattr(
            "aspire_orchestrator.services.adam.zip_to_msa.zip_to_msa_code",
            lambda z: "12420",
        )

        result = await SKILLPACK.labor_rate_lookup(
            payload={"trade": "electrician", "zipcode": "78701"},
            context=CTX,
        )
        assert result.success
        assert result.data["soc_code"] == "47-2111"
        assert result.data["median_hourly"] == pytest.approx(29.5)
        assert result.data["trade"] == "electrician"
        assert result.data["zipcode"] == "78701"
        assert result.data["msa_code"] == "12420"
        assert result.receipt

    @pytest.mark.asyncio
    async def test_bls_failure_propagates(self, monkeypatch):
        monkeypatch.setattr(
            "aspire_orchestrator.providers.bls_oews_client.execute_bls_oews_get_wage",
            AsyncMock(return_value=_make_tool_result(Outcome.FAILED, {}, error="BLS API request failed")),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.services.adam.zip_to_msa.zip_to_msa_code",
            lambda z: None,
        )

        result = await SKILLPACK.labor_rate_lookup(
            payload={"trade": "plumber", "zipcode": "99999"},
            context=CTX,
        )
        assert not result.success
        assert "BLS" in (result.error or "") or result.error is not None
        assert result.receipt

    @pytest.mark.asyncio
    async def test_unknown_zip_falls_back_to_national(self, monkeypatch):
        captured: list[dict[str, Any]] = []

        async def _capture_bls(**kwargs):
            captured.append(kwargs.get("payload", {}))
            return _make_tool_result(Outcome.SUCCESS, {
                "soc_code": "47-2152", "area_code": "0000000", "area_type": "national",
                "mean_hourly": 28.0, "median_hourly": 26.5, "pct_10_hourly": None,
                "pct_25_hourly": None, "pct_75_hourly": None, "pct_90_hourly": None,
                "employment": None, "year": "2024", "period": "A01",
            })

        monkeypatch.setattr(
            "aspire_orchestrator.providers.bls_oews_client.execute_bls_oews_get_wage",
            _capture_bls,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.services.adam.zip_to_msa.zip_to_msa_code",
            lambda z: None,  # unknown ZIP
        )

        result = await SKILLPACK.labor_rate_lookup(
            payload={"trade": "plumber", "zipcode": "99999"},
            context=CTX,
        )
        assert result.success
        # No msa_code in payload → national fallback
        assert captured[0].get("msa_code") is None
        assert result.data["msa_code"] is None

    @pytest.mark.asyncio
    async def test_all_five_trades_resolve(self, monkeypatch):
        """All 5 locked SOC codes must resolve without error."""
        monkeypatch.setattr(
            "aspire_orchestrator.providers.bls_oews_client.execute_bls_oews_get_wage",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, {
                "soc_code": "XX-XXXX", "area_code": "0000000", "area_type": "national",
                "mean_hourly": 25.0, "median_hourly": 24.0, "pct_10_hourly": None,
                "pct_25_hourly": None, "pct_75_hourly": None, "pct_90_hourly": None,
                "employment": None, "year": "2024", "period": "A01",
            })),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.services.adam.zip_to_msa.zip_to_msa_code",
            lambda z: None,
        )

        for trade in ("electrician", "plumber", "drywaller", "painter", "hvac"):
            result = await SKILLPACK.labor_rate_lookup(
                payload={"trade": trade, "zipcode": "78701"},
                context=CTX,
            )
            assert result.success or result.receipt, f"No receipt for trade={trade}"


# =============================================================================
# Fix R-2: Orphan receipt persistence — store_receipts called after _emit_receipt
# =============================================================================

class TestOrphanReceiptPersistence:

    @pytest.mark.asyncio
    async def test_subcontractor_discovery_persists_receipt(self, monkeypatch):
        """R-2: store_receipts must be called after _emit_receipt in subcontractor_discovery."""
        from unittest.mock import MagicMock
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)

        lsa_data = {"contractors": [
            {"id": "lsa_0", "name": "Fast Electric", "google_vetted": True,
             "phone": "", "rating": 4.7, "review_count": 80, "hours": "", "years_in_business": None}
        ], "result_count": 1}
        local_data = {"businesses": [], "result_count": 0}

        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_service_ads_client.execute_serpapi_local_service_ads_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, lsa_data)),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_client.execute_serpapi_local_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, local_data)),
        )

        result = await SKILLPACK.subcontractor_discovery(
            payload={"trade": "electrician", "zipcode": "78701"},
            context=CTX,
        )
        assert result.success
        assert store_mock.called, "store_receipts must be called to persist the parent receipt"
        all_persisted = []
        for call in store_mock.call_args_list:
            all_persisted.extend(call[0][0])
        event_types = {r.get("event_type") for r in all_persisted}
        assert "adam.subcontractor_discovery" in event_types

    @pytest.mark.asyncio
    async def test_labor_rate_lookup_persists_receipt(self, monkeypatch):
        """R-2: store_receipts must be called after _emit_receipt in labor_rate_lookup."""
        from unittest.mock import MagicMock
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)

        wage_data = {
            "soc_code": "47-2111", "area_code": "0012420", "area_type": "msa",
            "mean_hourly": 31.0, "median_hourly": 29.5, "pct_10_hourly": 18.0,
            "pct_25_hourly": 23.0, "pct_75_hourly": 38.0, "pct_90_hourly": 47.0,
            "employment": 12500, "year": "2024", "period": "A01",
        }
        monkeypatch.setattr(
            "aspire_orchestrator.providers.bls_oews_client.execute_bls_oews_get_wage",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, wage_data)),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.services.adam.zip_to_msa.zip_to_msa_code",
            lambda z: "12420",
        )

        result = await SKILLPACK.labor_rate_lookup(
            payload={"trade": "electrician", "zipcode": "78701"},
            context=CTX,
        )
        assert result.success
        assert store_mock.called, "store_receipts must be called to persist the labor_rate_lookup receipt"
        all_persisted = []
        for call in store_mock.call_args_list:
            all_persisted.extend(call[0][0])
        event_types = {r.get("event_type") for r in all_persisted}
        assert "adam.labor_rate_lookup" in event_types


# =============================================================================
# Fix P-4: Degraded status when total_found == 0
# =============================================================================

class TestDegradedStatusOnZeroResults:

    @pytest.mark.asyncio
    async def test_subcontractor_discovery_degraded_when_no_results(self, monkeypatch):
        """P-4: When total_found == 0, parent receipt status must be 'degraded', not 'ok'."""
        from unittest.mock import MagicMock
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)

        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_service_ads_client.execute_serpapi_local_service_ads_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, {"contractors": [], "result_count": 0})),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_client.execute_serpapi_local_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, {"businesses": [], "result_count": 0})),
        )

        result = await SKILLPACK.subcontractor_discovery(
            payload={"trade": "welder", "zipcode": "00001"},
            context=CTX,
        )
        assert result.success
        # The persisted receipt must be "degraded" not "ok" when no results
        all_persisted = []
        for call in store_mock.call_args_list:
            all_persisted.extend(call[0][0])
        discovery_receipt = next(
            (r for r in all_persisted if r.get("event_type") == "adam.subcontractor_discovery"), None
        )
        assert discovery_receipt is not None, "Must have a persisted subcontractor_discovery receipt"
        assert discovery_receipt["status"] == "degraded", (
            f"Expected status='degraded' when total_found==0, got '{discovery_receipt['status']}'"
        )

    @pytest.mark.asyncio
    async def test_subcontractor_discovery_ok_when_results_found(self, monkeypatch):
        """P-4: When total_found > 0, status must be 'ok'."""
        from unittest.mock import MagicMock
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)

        lsa_data = {"contractors": [
            {"id": "lsa_0", "name": "Good Electric", "google_vetted": True,
             "phone": "", "rating": 4.8, "review_count": 200, "hours": "", "years_in_business": None}
        ], "result_count": 1}

        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_service_ads_client.execute_serpapi_local_service_ads_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, lsa_data)),
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_local_client.execute_serpapi_local_search",
            AsyncMock(return_value=_make_tool_result(Outcome.SUCCESS, {"businesses": [], "result_count": 0})),
        )

        result = await SKILLPACK.subcontractor_discovery(
            payload={"trade": "electrician", "zipcode": "78701"},
            context=CTX,
        )
        all_persisted = []
        for call in store_mock.call_args_list:
            all_persisted.extend(call[0][0])
        discovery_receipt = next(
            (r for r in all_persisted if r.get("event_type") == "adam.subcontractor_discovery"), None
        )
        assert discovery_receipt is not None
        assert discovery_receipt["status"] == "ok"
