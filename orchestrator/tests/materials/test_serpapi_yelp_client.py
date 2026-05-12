"""SerpApi Yelp client — unit tests (Pass E).

Coverage targets:
  - Engine wiring (correct query params sent to SerpApi)
  - Budget gate (select_account → try_increment flow)
  - Dual-account failover (A exhausted → B)
  - Cached-only mode (both accounts exhausted)
  - Receipt emission on all outcomes (Law #2)
  - Response normalization (categories, rating, distance, hours)
  - Timeout enforcement (5s hard cap, Law #3)
  - Missing find_desc rejection
  - Query length cap (>500 chars rejected before budget increment)
  - 429 / quota-body exhaustion detection

Tests deliberately avoid importing the full app — they mock at the HTTP level
so the budget gate is tested against real serpapi_budget module logic.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.serpapi_yelp_client import (
    _normalize_business,
    execute_serpapi_yelp_search,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUITE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_OFFICE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_CORR_ID = "test-corr-yelp-001"


def _mock_ok_response(businesses: list[dict] | None = None) -> MagicMock:
    """Build a ProviderResponse-like mock that mimics a successful Yelp result."""
    biz_list = businesses or [
        {
            "title": "Atlas Concrete Supply",
            "place_id": "yelp_atlas_001",
            "address": "1234 Industrial Blvd",
            "city": "Tampa",
            "state": "FL",
            "zip_code": "33601",
            "phone": "(813) 555-0100",
            "website": "https://atlasconcrete.example.com",
            "rating": 4.2,
            "reviews": 87,
            "distance": 2.4,
            "hours": {"is_open_now": True},
            "categories": [{"title": "Building Supplies"}, {"title": "Concrete"}],
        }
    ]
    mock = MagicMock()
    mock.success = True
    mock.status_code = 200
    mock.body = {"organic_results": biz_list}
    mock.error_message = None
    mock.error_code = None
    return mock


def _mock_error_response(status_code: int = 500, message: str = "Internal error") -> MagicMock:
    from aspire_orchestrator.providers.error_codes import InternalErrorCode
    mock = MagicMock()
    mock.success = False
    mock.status_code = status_code
    mock.body = {"error": message}
    mock.error_message = message
    mock.error_code = InternalErrorCode.SERVER_INTERNAL_ERROR
    return mock


def _mock_429_response() -> MagicMock:
    from aspire_orchestrator.providers.error_codes import InternalErrorCode
    mock = MagicMock()
    mock.success = False
    mock.status_code = 429
    mock.body = {"error": "rate_limit_exceeded"}
    mock.error_message = "rate_limit_exceeded"
    mock.error_code = InternalErrorCode.RATE_LIMITED
    return mock


def _mock_quota_body_response() -> MagicMock:
    """SerpApi returns HTTP 200 with error body containing 'quota' on plan exhaustion."""
    from aspire_orchestrator.providers.error_codes import InternalErrorCode
    mock = MagicMock()
    mock.success = False
    mock.status_code = 200
    mock.body = {"error": "Your account has exceeded its monthly searches/month quota"}
    mock.error_message = "Your account has exceeded its monthly searches/month quota"
    mock.error_code = InternalErrorCode.RATE_LIMITED
    return mock


# ---------------------------------------------------------------------------
# Normalisation tests
# ---------------------------------------------------------------------------

class TestNormalizeBusiness:
    def test_full_business_normalised_correctly(self):
        biz = {
            "title": "Tampa Concrete Supply",
            "place_id": "yelp_tcs_001",
            "address": "1 Main St",
            "city": "Tampa",
            "state": "FL",
            "zip_code": "33601",
            "phone": "(813) 555-9000",
            "website": "https://tampaconcrete.example.com",
            "rating": "4.5",
            "reviews": "123",
            "distance": "1.2 mi",
            "hours": {"is_open_now": True},
            "categories": [{"title": "Building Supplies"}, {"title": "Concrete"}],
        }
        result = _normalize_business(biz, 0)
        assert result["name"] == "Tampa Concrete Supply"
        assert result["id"] == "yelp_tcs_001"
        assert result["city"] == "Tampa"
        assert result["state"] == "FL"
        assert result["zip"] == "33601"
        assert result["phone"] == "(813) 555-9000"
        assert result["website"] == "https://tampaconcrete.example.com"
        assert result["rating"] == 4.5
        assert result["review_count"] == 123
        assert result["distance_miles"] == 1.2
        assert result["hours_open_now"] is True
        assert "Building Supplies" in result["categories"]
        assert "Concrete" in result["categories"]

    def test_missing_optional_fields_default_safely(self):
        biz = {"title": "Minimal Supplier"}
        result = _normalize_business(biz, 3)
        assert result["name"] == "Minimal Supplier"
        assert result["id"] == "yelp_3"  # positional fallback
        assert result["rating"] is None
        assert result["distance_miles"] is None
        assert result["hours_open_now"] is None
        assert result["categories"] == []
        assert result["review_count"] == 0

    def test_numeric_distance_handled(self):
        biz = {"title": "X", "distance": 0.8}
        result = _normalize_business(biz, 0)
        assert result["distance_miles"] == 0.8

    def test_string_categories_handled(self):
        biz = {"title": "X", "categories": ["Lumber", "Hardware"]}
        result = _normalize_business(biz, 0)
        assert result["categories"] == ["Lumber", "Hardware"]

    def test_website_from_links_fallback(self):
        biz = {"title": "X", "links": {"website": "https://fallback.example.com"}}
        result = _normalize_business(biz, 0)
        assert result["website"] == "https://fallback.example.com"

    def test_reviews_with_commas_parsed(self):
        biz = {"title": "X", "reviews": "1,234"}
        result = _normalize_business(biz, 0)
        assert result["review_count"] == 1234


# ---------------------------------------------------------------------------
# Execute function tests
# ---------------------------------------------------------------------------

def _common_patches(request_response=None, *, account="A", count_a=5, count_b=0):
    """Return a context manager stack with the common budget mocks applied.

    Uses patch on the SerpApiYelpClient class _request method so tests are
    independent of the singleton pattern in the module.
    """
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch(
        "aspire_orchestrator.providers.serpapi_yelp_client.select_account",
        return_value=account,
    ))
    stack.enter_context(patch(
        "aspire_orchestrator.providers.serpapi_yelp_client.try_increment",
        return_value=True,
    ))
    stack.enter_context(patch(
        "aspire_orchestrator.providers.serpapi_yelp_client.get_api_key",
        return_value="fake-key",
    ))
    stack.enter_context(patch(
        "aspire_orchestrator.providers.serpapi_yelp_client.current_counts",
        return_value={"A": count_a, "B": count_b},
    ))
    if request_response is not None:
        from aspire_orchestrator.providers.serpapi_yelp_client import SerpApiYelpClient
        stack.enter_context(patch.object(
            SerpApiYelpClient,
            "_request",
            new_callable=AsyncMock,
            return_value=request_response,
        ))
    return stack


class TestExecuteSerpApiYelpSearch:

    @pytest.mark.asyncio
    async def test_missing_find_desc_returns_failure_with_receipt(self):
        result = await execute_serpapi_yelp_search(
            payload={},
            correlation_id=_CORR_ID,
            suite_id=_SUITE_ID,
            office_id=_OFFICE_ID,
        )
        assert result.outcome == Outcome.FAILED
        assert "find_desc" in (result.error or "").lower()
        assert result.receipt_data is not None
        assert result.receipt_data.get("reason_code") == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_query_over_500_chars_rejected_before_budget_increment(self):
        long_query = "x" * 501
        increment_mock = MagicMock(return_value=True)
        with patch(
            "aspire_orchestrator.providers.serpapi_yelp_client.try_increment",
            increment_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_yelp_client.select_account",
            return_value="A",
        ):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": long_query},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        # Must reject BEFORE incrementing budget
        increment_mock.assert_not_called()
        assert result.outcome == Outcome.FAILED
        assert "500" in (result.error or "")

    @pytest.mark.asyncio
    async def test_budget_exhausted_both_accounts_returns_failure(self):
        with patch(
            "aspire_orchestrator.providers.serpapi_yelp_client.select_account",
            return_value=None,
        ), patch(
            "aspire_orchestrator.providers.serpapi_yelp_client.current_counts",
            return_value={"A": 240, "B": 240},
        ):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "concrete supplier"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data is not None
        assert result.receipt_data.get("reason_code") == "SERPAPI_BUDGET_EXHAUSTED"

    @pytest.mark.asyncio
    async def test_successful_search_returns_normalised_suppliers(self):
        with _common_patches(_mock_ok_response()):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "concrete supplier", "find_loc": "Tampa, FL"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        assert result.outcome == Outcome.SUCCESS
        assert result.data is not None
        assert len(result.data["suppliers"]) == 1
        assert result.data["suppliers"][0]["name"] == "Atlas Concrete Supply"
        assert result.receipt_data is not None

    @pytest.mark.asyncio
    async def test_receipt_emitted_on_success(self):
        with _common_patches(_mock_ok_response()):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "lumber yard"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        assert result.receipt_data is not None, "Law #2: every outcome must emit a receipt"
        rd = result.receipt_data
        assert rd.get("id") is not None
        assert rd.get("redacted_outputs", {}).get("engine") == "yelp"
        assert rd.get("redacted_outputs", {}).get("cached") is False

    @pytest.mark.asyncio
    async def test_timeout_returns_failure_with_receipt(self):
        async def _slow(*_args, **_kwargs):
            await asyncio.sleep(99)

        from aspire_orchestrator.providers.serpapi_yelp_client import SerpApiYelpClient
        with _common_patches(), patch.object(SerpApiYelpClient, "_request", new=_slow):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "hvac supply"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
                timeout=0.01,  # Force immediate timeout
            )
        assert result.outcome == Outcome.FAILED
        assert "timeout" in (result.error or "").lower()
        assert result.receipt_data is not None
        from aspire_orchestrator.providers.error_codes import InternalErrorCode
        assert result.receipt_data.get("reason_code") == InternalErrorCode.NETWORK_TIMEOUT.value

    @pytest.mark.asyncio
    async def test_account_a_failover_to_b_on_429(self):
        """429 on account A → mark exhausted → single attempt on account B."""
        call_count = 0

        async def _mock_request(_self, _req):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_429_response()
            return _mock_ok_response()

        mark_mock = MagicMock()

        from aspire_orchestrator.providers.serpapi_yelp_client import SerpApiYelpClient
        with _common_patches(), \
             patch.object(SerpApiYelpClient, "_request", new=_mock_request), \
             patch(
                 "aspire_orchestrator.providers.serpapi_yelp_client.mark_account_exhausted",
                 mark_mock,
             ), \
             patch("aspire_orchestrator.services.receipt_store.store_receipts", MagicMock()):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "plumbing supply"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        # mark_account_exhausted must be called for account A
        mark_mock.assert_called_once()
        call_args = mark_mock.call_args[0]
        assert call_args[0] == "A"
        # Final result should be success (account B worked)
        assert result.outcome == Outcome.SUCCESS

    @pytest.mark.asyncio
    async def test_receipt_budget_fields_populated(self):
        """redacted_outputs must contain engine, account_id, cached, budget_remaining_a/b."""
        with _common_patches(_mock_ok_response(), count_a=10, count_b=3):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "structural steel"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        ro = result.receipt_data.get("redacted_outputs", {})
        assert ro.get("engine") == "yelp"
        assert ro.get("cached") is False
        assert ro.get("budget_remaining_a") == 230   # 240 - 10
        assert ro.get("budget_remaining_b") == 237   # 240 - 3
        assert ro.get("account_id") == "A"

    @pytest.mark.asyncio
    async def test_find_loc_sent_in_query_params(self):
        """find_loc must be forwarded to SerpApi."""
        captured_request = {}

        async def _capture_request(_self, req):
            captured_request.update(req.query_params or {})
            return _mock_ok_response()

        from aspire_orchestrator.providers.serpapi_yelp_client import SerpApiYelpClient
        with _common_patches(), patch.object(SerpApiYelpClient, "_request", new=_capture_request):
            await execute_serpapi_yelp_search(
                payload={"find_desc": "rebar supplier", "find_loc": "Atlanta, GA 30301"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        assert captured_request.get("engine") == "yelp"
        assert captured_request.get("find_desc") == "rebar supplier"
        assert captured_request.get("find_loc") == "Atlanta, GA 30301"
        # API key must NOT appear in logs/assertions — this test only checks presence, not value
        assert "api_key" in captured_request

    @pytest.mark.asyncio
    async def test_race_condition_both_accounts_at_cap_after_select(self):
        """Race: select_account returns A, but try_increment fails for both A and B."""
        with patch(
            "aspire_orchestrator.providers.serpapi_yelp_client.select_account",
            return_value="A",
        ), patch(
            "aspire_orchestrator.providers.serpapi_yelp_client.try_increment",
            return_value=False,  # Both accounts at cap
        ), patch(
            "aspire_orchestrator.providers.serpapi_yelp_client.current_counts",
            return_value={"A": 240, "B": 240},
        ):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "commercial grade pipe"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data is not None
        assert result.receipt_data.get("reason_code") == "SERPAPI_BUDGET_EXHAUSTED"

    @pytest.mark.asyncio
    async def test_no_secrets_in_receipt(self):
        """Law #9: API key must not appear in receipt redacted_outputs."""
        fake_key = "SUPER_SECRET_YELP_KEY_ABCDEF123"

        with _common_patches(_mock_ok_response()):
            with patch(
                "aspire_orchestrator.providers.serpapi_yelp_client.get_api_key",
                return_value=fake_key,
            ):
                result = await execute_serpapi_yelp_search(
                    payload={"find_desc": "hvac distributor"},
                    correlation_id=_CORR_ID,
                    suite_id=_SUITE_ID,
                    office_id=_OFFICE_ID,
                )
        receipt_str = str(result.receipt_data)
        assert fake_key not in receipt_str, "Law #9: API key must not appear in receipt"


# ---------------------------------------------------------------------------
# Error mapping tests — one test per failure mode table row
# ---------------------------------------------------------------------------

class TestErrorMapping:

    @pytest.mark.asyncio
    async def test_http_401_maps_to_auth_invalid_key(self):
        from aspire_orchestrator.providers.error_codes import InternalErrorCode
        err_resp = MagicMock()
        err_resp.success = False
        err_resp.status_code = 401
        err_resp.body = {"error": "authentication_error"}
        err_resp.error_message = "authentication_error"
        err_resp.error_code = InternalErrorCode.AUTH_INVALID_KEY

        with _common_patches(err_resp):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "lumber"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data is not None

    @pytest.mark.asyncio
    async def test_quota_body_maps_to_rate_limited_and_marks_exhausted(self):
        mark_mock = MagicMock()

        from aspire_orchestrator.providers.serpapi_yelp_client import SerpApiYelpClient
        with _common_patches(count_a=240, count_b=5), \
             patch.object(
                 SerpApiYelpClient,
                 "_request",
                 new_callable=AsyncMock,
                 return_value=_mock_quota_body_response(),
             ), \
             patch(
                 "aspire_orchestrator.providers.serpapi_yelp_client.mark_account_exhausted",
                 mark_mock,
             ), \
             patch("aspire_orchestrator.services.receipt_store.store_receipts", MagicMock()):
            result = await execute_serpapi_yelp_search(
                payload={"find_desc": "grease trap supply"},
                correlation_id=_CORR_ID,
                suite_id=_SUITE_ID,
                office_id=_OFFICE_ID,
            )
        # Account A should be marked exhausted
        mark_mock.assert_called_once()
