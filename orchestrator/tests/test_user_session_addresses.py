"""b269e5ff user session regression locks.

Pin the 3 addresses the user tested live in the Round 2 session that triggered
the Wave A fix. Any of these returning artifact_type=error (the legacy fallback
behavior) is a hard regression.

Mode: same as test_attom_real_addresses.py — VCR cassettes recorded once
against live ATTOM, replayed for CI determinism.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

vcr = pytest.importorskip("vcr", reason="vcrpy required for ATTOM cassette replay")

CASSETTE_DIR = Path(__file__).parent / "cassettes" / "user_session_b269e5ff"
CASSETTE_DIR.mkdir(parents=True, exist_ok=True)

RECORD_LIVE = os.environ.get("ATTOM_RECORD_LIVE", "").lower() in {"1", "true", "yes"}


@pytest.fixture(scope="module")
def my_vcr() -> Any:
    return vcr.VCR(
        record_mode=("new_episodes" if RECORD_LIVE else "none"),
        filter_headers=[("apikey", "REDACTED-ATTOM-KEY"), "authorization"],
        filter_query_parameters=[("apikey", "REDACTED")],
        match_on=["method", "scheme", "host", "path", "query"],
        decode_compressed_response=True,
    )


@pytest.fixture
def adam_ctx() -> Any:
    from aspire_orchestrator.services.adam.schemas.playbook_context import (
        PlaybookContext,
    )

    return PlaybookContext(
        suite_id="00000000-0000-0000-0000-000000000001",
        office_id="00000000-0000-0000-0000-000000000001",
        correlation_id="test-b269e5ff-regression",
    )


def _maybe_skip(cassette: Path) -> None:
    if not RECORD_LIVE and not cassette.exists():
        pytest.skip(
            f"cassette not yet recorded: {cassette.name}. "
            "Set ATTOM_RECORD_LIVE=1 + ASPIRE_ATTOM_API_KEY to seed."
        )


@pytest.mark.asyncio
async def test_session_1575_paul_russell_unit_4802(
    my_vcr: Any, adam_ctx: Any
) -> None:
    """Hero address from b269e5ff — must resolve unit-level data."""
    cassette = CASSETTE_DIR / "01_1575_paul_russell_apt_4802.yaml"
    _maybe_skip(cassette)

    from aspire_orchestrator.services.adam.playbooks.landlord import (
        execute_property_facts,
    )

    with my_vcr.use_cassette(str(cassette)):
        research = await execute_property_facts(
            query="1575 Paul Russell Road, apartment 4802, Tallahassee, FL 32301",
            context=adam_ctx,
        )

    assert research.artifact_type != "error", (
        f"REGRESSION: legacy artifact_type=error path resurfaced. "
        f"summary={research.summary!r}"
    )
    assert research.artifact_type != "needs_more_input", (
        f"address parsed cleanly, but pipeline asked for more input — "
        f"summary={research.summary!r}"
    )
    assert research.records, "no property records returned"

    rec = research.records[0]
    living_sqft = rec.get("living_sqft") or 0
    tax_market_value = rec.get("tax_market_value") or 0

    assert int(living_sqft) >= 1500, (
        f"living_sqft below unit-level threshold: got {living_sqft!r}"
    )
    assert float(tax_market_value) > 50000, (
        f"tax_market_value below unit-level threshold: got {tax_market_value!r}"
    )


@pytest.mark.asyncio
async def test_session_604_ward_pl_forest_park(
    my_vcr: Any, adam_ctx: Any
) -> None:
    """Forest Park GA SFR — must produce bedrooms_count + tax_market_value."""
    cassette = CASSETTE_DIR / "02_604_ward_pl_forest_park.yaml"
    _maybe_skip(cassette)

    from aspire_orchestrator.services.adam.playbooks.landlord import (
        execute_property_facts,
    )

    with my_vcr.use_cassette(str(cassette)):
        research = await execute_property_facts(
            query="604 Ward Pl, Forest Park, GA 30297",
            context=adam_ctx,
        )

    assert research.artifact_type != "error", (
        f"REGRESSION: legacy artifact_type=error path resurfaced. "
        f"summary={research.summary!r}"
    )
    assert research.records, "no property records returned"

    rec = research.records[0]
    assert rec.get("beds") is not None, "bedrooms_count missing"
    assert rec.get("tax_market_value") is not None, "tax_market_value missing"
    assert float(rec.get("tax_market_value") or 0) > 0, (
        f"tax_market_value zero or negative: {rec.get('tax_market_value')}"
    )


@pytest.mark.asyncio
async def test_session_4863_price_st_forest_park(
    my_vcr: Any, adam_ctx: Any
) -> None:
    """Forest Park GA SFR — full property profile must populate."""
    cassette = CASSETTE_DIR / "03_4863_price_st_forest_park.yaml"
    _maybe_skip(cassette)

    from aspire_orchestrator.services.adam.playbooks.landlord import (
        execute_property_facts,
    )

    with my_vcr.use_cassette(str(cassette)):
        research = await execute_property_facts(
            query="4863 Price St, Forest Park, GA 30297",
            context=adam_ctx,
        )

    assert research.artifact_type != "error", (
        f"REGRESSION: legacy artifact_type=error path resurfaced. "
        f"summary={research.summary!r}"
    )
    assert research.records, "no property records returned"

    rec = research.records[0]
    for required_field in (
        "normalized_address",
        "living_sqft",
        "year_built",
        "beds",
        "tax_market_value",
        "property_type",
    ):
        assert rec.get(required_field) is not None, (
            f"required field {required_field!r} missing in PropertyFactPack"
        )
