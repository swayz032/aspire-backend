"""Unit tests for landlord address specificity and ATTOM subject pinning."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.playbooks import landlord
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext


def _ctx() -> PlaybookContext:
    return PlaybookContext(
        suite_id="00000000-0000-0000-0000-000000000001",
        office_id="00000000-0000-0000-0000-000000000001",
        correlation_id="test-corr",
    )


@pytest.mark.asyncio
async def test_geocode_keeps_raw_when_here_drops_house_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """If HERE degrades to street-only address, preserve original house-number address."""

    async def _fake_here_search(*, payload, **_kwargs):
        return SimpleNamespace(
            outcome=Outcome.SUCCESS,
            data={"results": [{"address": "Price St, Forest Park, GA 30297"}]},
            error=None,
        )

    monkeypatch.setattr(landlord, "execute_here_search", _fake_here_search)
    query = "Give me property facts for 4863 Price St, Forest Park, GA 30297"
    resolved = await landlord._geocode_address(query, _ctx())
    assert resolved.startswith("4863 ")


def test_pin_attom_payload_chooses_exact_house_number_match() -> None:
    """When ATTOM returns multiple properties on a street, pin to subject address."""
    payload = {
        "property": [
            {"address": {"oneLine": "PRICE ST, FOREST PARK, GA 30297"}, "identifier": {"attomId": "111"}},
            {"address": {"oneLine": "4863 PRICE ST, FOREST PARK, GA 30297"}, "identifier": {"attomId": "222"}},
        ]
    }
    pinned = landlord._pin_attom_payload_to_subject(payload, "4863 Price St, Forest Park, GA 30297")
    assert pinned
    assert pinned["property"][0]["identifier"]["attomId"] == "222"

