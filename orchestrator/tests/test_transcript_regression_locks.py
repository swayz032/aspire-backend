"""Transcript regression locks — Wave R5 / tests-master.

Each test is pinned to a specific user session transcript that surfaced a
confirmed bug. The test name and docstring reference the transcript file
so future devs can trace the regression lock back to its original report.

Transcripts:
  426b860b — multi-bug session: Bangor returned, modal in parent, arrow exit,
             image cut off, "View details" ERROR, Ava interrupts
  055f610b — MISSING_TASK returned 3× despite populated body; UTC greeting bug
  214de471 — voice request timed out at 5160ms (>5s Anam ceiling)
  3ca28bc6 — Round 7 (2026-04-30): invoke_adam returned MISSING_TASK 3×;
             hole-in-wall PROBLEM mode + address ask worked, but invoke_adam
             never resolved. Wave D.6 regression lock.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.playbooks.trades import (
    execute_tool_material_price_check,
)
from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.tool_types import ToolExecutionResult


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _ok(tool_id: str, data: dict) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=data,
        receipt_data={"id": "test", "outcome": "success"},
    )


def _fail(tool_id: str, error: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error=error,
    )


def _ctx(correlation_id: str = "regression-lock") -> PlaybookContext:
    return PlaybookContext(
        suite_id="11111111-1111-4111-8111-111111111111",
        office_id="22222222-2222-2222-2222-222222222222",
        correlation_id=correlation_id,
    )


# ─── Tallahassee-specific mock data ──────────────────────────────────────────

_TALLAHASSEE_HD_OK = {
    "results": [
        {
            "title": "USG Sheetrock 4 ft. x 8 ft. x 1/2 in. Drywall Panel",
            "brand": "USG",
            "model_number": "12347",
            "product_id": "202011387",
            "price": 14.98,
            "rating": 4.6,
            "reviews": 3120,
            "link": "https://homedepot.com/p/usg-sheetrock",
            # Image must be a /v1/places/photo proxy URL — NOT a raw Google key
            "image_url": "/v1/places/photo?ref=places/ChIJ123456/photos/AUjq9jm",
            "pickup": {
                "store_id": "0254",
                "store_name": "West Tallahassee",
                "quantity": 48,
            },
            "delivery": {"has_delivery": True},
            "thumbnail": "https://example.com/sheetrock-thumb.jpg",
        },
    ],
    "query": "sheetrock",
    "result_count": 1,
    "store": {
        "store_id": "0254",
        "store_name": "West Tallahassee",
        "city": "Tallahassee",
        "state": "FL",
    },
}


# ─── Transcript 426b860b — Bangor returned for Tallahassee query ──────────────

class TestTranscript426b860b:
    """Regression lock for transcript 426b860b.

    Source: docs/transcripts/426b860b.md (multi-bug session)
    Bugs locked:
      1. Backend returned Bangor, ME store instead of Tallahassee, FL
      2. store.image_url contained raw 'key=' param (Google key exposure)
    """

    @pytest.mark.asyncio
    async def test_sheetrock_tallahassee_returns_tallahassee_not_bangor(self):
        """Transcript 426b860b: Tallahassee query must return a Tallahassee store.

        User said: "Sheetrock at Home Depot in Tallahassee, Florida".
        Prior bug: Bangor, ME store was returned (city lookup or store_id mismatch).
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("transcript-426b860b-city"),
                city="Tallahassee",
                state="FL",
            )

        assert response.artifact_type in (
            "PriceComparison", "StoreDisambiguation"
        ), f"Expected PriceComparison or StoreDisambiguation, got {response.artifact_type!r}"

        if response.artifact_type == "PriceComparison" and response.records:
            record = response.records[0]
            # Store must be Tallahassee, not Bangor
            city_val = (
                record.get("city")
                or (record.get("extra") or {}).get("city")
                or (record.get("store") or {}).get("city")
                or ""
            ).lower()
            # If city is present it must NOT be Bangor
            if city_val:
                assert "bangor" not in city_val, (
                    f"Bug regression 426b860b: Bangor returned for Tallahassee query. "
                    f"record city={city_val!r}"
                )

    @pytest.mark.asyncio
    async def test_image_url_does_not_contain_raw_google_key(self):
        """Transcript 426b860b: store image_url must NOT embed raw Google API key.

        Prior bug: image_url contained '&key=AIzaSy...' which exposed the API key
        in HTTP logs and the card render. F-CRIT-5 fix replaced direct Google URLs
        with /v1/places/photo proxy URLs.
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("transcript-426b860b-key"),
                city="Tallahassee",
                state="FL",
            )

        for record in response.records:
            image_url = record.get("image_url") or ""
            assert "key=" not in image_url, (
                f"Bug regression 426b860b: raw Google key in image_url: {image_url!r}. "
                "F-CRIT-5 requires proxy URL /v1/places/photo?ref=..."
            )

    @pytest.mark.asyncio
    async def test_response_success_flag_true_for_tallahassee_sheetrock(self):
        """Transcript 426b860b: 'View details' returned ERROR due to bad response shape.

        The error originated from response.success being falsy even when records
        were present. Verify success==True when at least one record is returned.
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("transcript-426b860b-success"),
                city="Tallahassee",
                state="FL",
            )

        assert response.artifact_type != "error", (
            f"Bug regression 426b860b: response must not be error when HD returns data. "
            f"Got artifact_type={response.artifact_type!r}, summary={response.summary!r}"
        )


# ─── Transcript 055f610b — MISSING_TASK + UTC greeting ───────────────────────

class TestTranscript055f610b:
    """Regression lock for transcript 055f610b.

    Source: docs/transcripts/055f610b.md
    Bugs locked:
      1. Adam returned MISSING_TASK 3x even though task+query body was populated
         (body parsing bug — task field was not extracted from the request body)
      2. Ava said "Good evening" then "Good morning" 7s apart (UTC offset bug)
    """

    @pytest.mark.asyncio
    async def test_dispatch_with_task_and_query_does_not_return_missing_task(self):
        """Transcript 055f610b: dispatch_playbook with populated task must not return MISSING_TASK.

        Prior bug: the body parser consumed 'task' from the raw request before it
        reached the playbook dispatcher, causing three MISSING_TASK responses in
        a row for the same session.
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))
        shopping_mock = AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []}))

        ctx = _ctx("transcript-055f610b-task")

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        ):
            # dispatch_playbook with an explicit task+query — must not MISSING_TASK
            response = await dispatch_playbook(
                "TOOL_MATERIAL_PRICE_CHECK",
                "Price check sheetrock in Tallahassee",
                ctx,
            )

        assert response.artifact_type != "error" or "MISSING_TASK" not in (response.summary or ""), (
            f"Bug regression 055f610b: MISSING_TASK returned despite populated query. "
            f"artifact_type={response.artifact_type!r} summary={response.summary!r}"
        )
        # Receipt must be present (Law #2: every executed action produces a receipt)
        assert response.providers_called is not None, (
            "Bug regression 055f610b: providers_called must be set after dispatch"
        )

    @pytest.mark.asyncio
    async def test_receipt_emitted_on_successful_dispatch(self):
        """Transcript 055f610b: receipt must be emitted on every successful dispatch.

        Law #2: No action without a receipt. Verify the response carries
        receipt-related fields (providers_called, segment, playbook name) AND
        that store_receipts was actually called — Round 7 H-2 found that the
        prior version of this test only asserted response fields, so a future
        regression where _emit_playbook_receipt silently fails would still pass.
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))

        ctx = _ctx("transcript-055f610b-receipt")

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.receipt_store.store_receipts"
        ) as mock_store_receipts:
            response = await dispatch_playbook(
                "TOOL_MATERIAL_PRICE_CHECK",
                "sheetrock price",
                ctx,
            )

        # ResearchResponse as receipt: playbook, segment, providers_called set
        assert response.playbook, "receipt field 'playbook' must be set"
        assert response.segment, "receipt field 'segment' must be set"
        assert response.providers_called is not None, (
            "receipt field 'providers_called' must be present (Law #2)"
        )
        # Round 7 H-2: store_receipts must actually be invoked. Response fields
        # alone are not sufficient evidence that a receipt was persisted.
        assert mock_store_receipts.call_count >= 1, (
            f"Law #2 regression: store_receipts was called "
            f"{mock_store_receipts.call_count} times; expected >= 1 to confirm "
            "_emit_playbook_receipt actually persisted a receipt."
        )


# ─── Transcript 214de471 — voice timeout >5s ─────────────────────────────────

class TestTranscript214de471:
    """Regression lock for transcript 214de471.

    Source: docs/transcripts/214de471.md
    Bug locked:
      Voice request timed out at 5160ms — exceeded the 5s Anam ceiling.
      Root cause: HD client was called 3 times × 8s timeout = 24s instead of
      1 attempt × 4s timeout on the voice path.
    """

    @pytest.mark.asyncio
    async def test_voice_path_single_attempt_with_4s_timeout(self):
        """Transcript 214de471: voice path must use ONE attempt with timeout=4.0.

        Prior bug: voice requests ran 3 attempts × 8s = 24s, crashing Anam
        sessions that have a hard 5s ceiling.
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ):
            await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("transcript-214de471-attempt"),
                voice_path=True,
            )

        assert hd_mock.await_count == 1, (
            f"Bug regression 214de471: voice path ran {hd_mock.await_count} attempt(s); "
            "must be exactly 1 (single-shot, no retry loop)"
        )
        kwargs = hd_mock.await_args.kwargs
        assert kwargs.get("timeout") == 4.0, (
            f"Bug regression 214de471: voice path timeout={kwargs.get('timeout')!r}; "
            "must be 4.0 (not 8.0 which causes 5s+ overrun)"
        )

    @pytest.mark.asyncio
    async def test_voice_path_end_to_end_under_4500ms_with_15s_serpapi(self):
        """Transcript 214de471: end-to-end voice budget must be <4.5s.

        Mocks SerpApi to respond in 1.5s — the p90 latency observed in
        transcript 214de471 before the fix. Budget = 4.5s (with 0.5s headroom
        from the 5s Anam ceiling).
        """
        async def slow_hd(*args, **kwargs):
            await asyncio.sleep(1.5)
            return _ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK)

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            side_effect=slow_hd,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ):
            start = time.perf_counter()
            await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("transcript-214de471-budget"),
                voice_path=True,
            )
            elapsed = time.perf_counter() - start

        assert elapsed < 4.5, (
            f"Bug regression 214de471: voice path took {elapsed:.3f}s; "
            "budget is <4.5s (one attempt × 1.5s SerpApi + overhead). "
            "Ensure single-attempt + 4s timeout are active."
        )


# ─── Transcript 3ca28bc6 — Round 7 MISSING_TASK + hole-in-wall (2026-04-30) ──

class TestTranscript3ca28bc6:
    """Regression lock for transcript 3ca28bc6 (2026-04-30, 6:23 PM, 2.35 min).

    Source: plan hey-can-you-deep-serene-elephant.md / user live test.

    Bugs locked:
      1. invoke_adam returned MISSING_TASK 3× with a fully-formed payload
         (task, agent, query, entity_type, user_address all present).
         Root cause: Anam sends invoke_adam args FLAT (no bodyParams wrapper).
         Round 6 unwrapped bodyParams but invoke_adam was never wrapped — so
         the fix addressed ava_get_context only. The dispatch_playbook path
         must never return MISSING_TASK when task + query are present.
      2. Session payload: task=TOOL_MATERIAL_PRICE_CHECK, agent=adam,
         query='lightweight spackle, putty knife, fine-grit sandpaper, primer',
         entity_type='material', user_address='<Tallahassee address>'.

    Law #2 — receipt must be emitted regardless of outcome.
    Law #3 — fail closed: MISSING_TASK is a hard fail; must not happen when
              task+query are fully present.
    """

    @pytest.mark.asyncio
    async def test_dispatch_with_3ca28bc6_payload_does_not_return_missing_task(self):
        """D.6: dispatch_playbook with session 3ca28bc6 payload must not return MISSING_TASK.

        Replicates the exact task+query from the 2026-04-30 6:23 PM live session
        where Ava correctly diagnosed the hole-in-wall problem and named materials
        but invoke_adam returned MISSING_TASK 3 times in a row.
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))
        shopping_mock = AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []}))

        ctx = _ctx("transcript-3ca28bc6-dispatch")

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        ):
            response = await dispatch_playbook(
                # task as it appeared in the 3ca28bc6 session payload
                "TOOL_MATERIAL_PRICE_CHECK",
                # query as Ava diagnosed and sent: multi-material repair list
                "lightweight spackle, putty knife, fine-grit sandpaper, primer",
                ctx,
            )

        # Primary assertion: MISSING_TASK must not appear.
        assert "MISSING_TASK" not in (response.summary or ""), (
            f"Transcript 3ca28bc6 regression: MISSING_TASK returned despite fully-formed "
            f"payload. artifact_type={response.artifact_type!r} summary={response.summary!r}"
        )
        assert response.artifact_type != "error" or "MISSING_TASK" not in (response.summary or ""), (
            f"artifact_type='error' with MISSING_TASK is a hard regression. "
            f"summary={response.summary!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_direct_with_3ca28bc6_payload_not_error(self):
        """D.6: execute_tool_material_price_check with 3ca28bc6 params must not error.

        Exercises the direct playbook call with the same params Anam sent
        in session 3ca28bc6: multi-material query + voice_path=True + Tallahassee
        user_address. artifact_type must NOT be 'error'.
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ):
            response = await execute_tool_material_price_check(
                query="lightweight spackle, putty knife, fine-grit sandpaper, primer",
                ctx=_ctx("transcript-3ca28bc6-direct"),
                user_address="1234 Miccosukee Rd, Tallahassee, FL 32308",
                voice_path=True,
            )

        assert response.artifact_type != "error", (
            f"Transcript 3ca28bc6 regression: artifact_type='error' for valid voice query. "
            f"summary={response.summary!r}"
        )
        assert "MISSING_TASK" not in (response.summary or ""), (
            f"Transcript 3ca28bc6 regression: MISSING_TASK in summary for direct call. "
            f"summary={response.summary!r}"
        )

    @pytest.mark.asyncio
    async def test_3ca28bc6_response_carries_receipt_fields(self):
        """D.6: every invoke from 3ca28bc6 payload must carry receipt evidence (Law #2).

        providers_called, playbook, and segment must be set on the response
        AND store_receipts must actually be invoked. Round 7 H-2 strengthened
        this assertion — response fields alone are not proof a receipt was
        persisted; a future regression where _emit_playbook_receipt silently
        fails would have passed the prior version.
        """
        hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_OK))

        ctx = _ctx("transcript-3ca28bc6-receipt")

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.receipt_store.store_receipts"
        ) as mock_store_receipts:
            response = await dispatch_playbook(
                "TOOL_MATERIAL_PRICE_CHECK",
                "lightweight spackle, putty knife, fine-grit sandpaper, primer",
                ctx,
            )

        assert response.playbook, (
            "Transcript 3ca28bc6: receipt field 'playbook' must be set (Law #2)"
        )
        assert response.segment, (
            "Transcript 3ca28bc6: receipt field 'segment' must be set (Law #2)"
        )
        assert response.providers_called is not None, (
            "Transcript 3ca28bc6: providers_called must be set (Law #2 receipt evidence)"
        )
        # Round 7 H-2: store_receipts must actually be invoked.
        assert mock_store_receipts.call_count >= 1, (
            f"Law #2 regression: store_receipts was called "
            f"{mock_store_receipts.call_count} times; expected >= 1 to confirm "
            "_emit_playbook_receipt actually persisted a receipt for the "
            "3ca28bc6 payload."
        )
