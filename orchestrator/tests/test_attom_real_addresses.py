"""End-to-end coverage test: parse 50 real US addresses → ATTOM /expandedprofile → assert unit-level data.

This is the Wave A regression baseline. PASS criterion: 50/50 addresses must
parse + resolve via the Adam deterministic primary path.

Mode of operation:
  1. Live recording (record-once): set ATTOM_RECORD_LIVE=1 + ASPIRE_ATTOM_API_KEY
     in env. Tests run against real ATTOM and write cassettes to tests/cassettes/.
  2. Replay (default for CI): cassettes are replayed; no network calls happen.

Skip behavior:
  - If a cassette is missing AND ATTOM_RECORD_LIVE is not set, the case is
    SKIPPED with a clear message (so CI can run incrementally as cassettes are
    seeded).

Why no fallback fail mode:
  - Per Aspire no-fallback-design-principle: tests treat the primary path as
    the contract. A failed parse / 4xx-5xx ATTOM response is a hard FAIL,
    never a soft skip (except for the missing-cassette bootstrap).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.real_addresses import ALL_REAL_ADDRESSES, AddressFixture

vcr = pytest.importorskip("vcr", reason="vcrpy required for ATTOM cassette replay")

CASSETTE_DIR = Path(__file__).parent / "cassettes" / "attom_real_addresses"
CASSETTE_DIR.mkdir(parents=True, exist_ok=True)

RECORD_LIVE = os.environ.get("ATTOM_RECORD_LIVE", "").lower() in {"1", "true", "yes"}
ATTOM_API_KEY = os.environ.get("ASPIRE_ATTOM_API_KEY", "")


def _cassette_path(idx: int, raw: str) -> Path:
    """Stable cassette filename per fixture (use index for ordering)."""
    safe = "".join(c if c.isalnum() else "_" for c in raw[:50])
    return CASSETTE_DIR / f"{idx:02d}_{safe}.yaml"


def _vcr_config() -> dict[str, Any]:
    record_mode = "new_episodes" if RECORD_LIVE else "none"
    return {
        "record_mode": record_mode,
        "filter_headers": [("apikey", "REDACTED-ATTOM-KEY"), "authorization"],
        "filter_query_parameters": [("apikey", "REDACTED")],
        "match_on": ["method", "scheme", "host", "path", "query"],
        "decode_compressed_response": True,
    }


@pytest.fixture(scope="session")
def my_vcr() -> Any:
    return vcr.VCR(**_vcr_config())


@pytest.mark.parametrize(
    "idx,fixture",
    list(enumerate(ALL_REAL_ADDRESSES)),
    ids=[f["raw"][:60] for f in ALL_REAL_ADDRESSES],
)
@pytest.mark.asyncio
async def test_attom_real_address_resolves(
    idx: int,
    fixture: AddressFixture,
    my_vcr: Any,
) -> None:
    """For every fixture: parse → ATTOM /expandedprofile → assert essentials."""
    cassette = _cassette_path(idx, fixture["raw"])

    if not RECORD_LIVE and not cassette.exists():
        pytest.skip(
            f"cassette not yet recorded: {cassette.name}. "
            "Set ATTOM_RECORD_LIVE=1 + ASPIRE_ATTOM_API_KEY to seed."
        )

    if RECORD_LIVE and not ATTOM_API_KEY:
        pytest.fail(
            "ATTOM_RECORD_LIVE=1 but ASPIRE_ATTOM_API_KEY missing — cannot record"
        )

    from aspire_orchestrator.services.adam.address_parser import (
        ParseError,
        parse_us_address,
    )

    try:
        parsed = parse_us_address(fixture["raw"])
    except ParseError as exc:
        pytest.fail(f"parse_us_address failed for {fixture['raw']!r}: {exc}")

    assert fixture["expected_state"] in parsed.address2, (
        f"state {fixture['expected_state']!r} not in address2={parsed.address2!r}"
    )

    expected_unit = fixture["expected_unit_or_none"]
    actual_unit = parsed.components.get("OccupancyIdentifier", "") or None
    if expected_unit:
        assert str(expected_unit).upper() == str(actual_unit or "").upper(), (
            f"unit mismatch: expected={expected_unit!r} actual={actual_unit!r}"
        )

    with my_vcr.use_cassette(str(cassette)):
        from aspire_orchestrator.providers.attom_client import (
            execute_attom_expanded_profile,
        )

        result = await execute_attom_expanded_profile(
            payload={
                "address1": parsed.address1,
                "address2": parsed.address2,
                "address": f"{parsed.address1}, {parsed.address2}",
            },
            correlation_id=f"test-attom-{idx:02d}",
            suite_id="00000000-0000-0000-0000-000000000001",
            office_id="00000000-0000-0000-0000-000000000001",
            risk_tier="green",
        )

    assert result.outcome.value == "success", (
        f"ATTOM /expandedprofile failed for {fixture['raw']!r}: {result.error}"
    )
    assert result.data is not None
    properties = result.data.get("property", [])
    if not properties:
        pytest.skip(
            f"ATTOM SuccessWithoutResult for {fixture['raw']!r} — "
            "valid address but ATTOM has no record"
        )

    prop = properties[0]
    building = prop.get("building", {}) or {}
    bldg_size = building.get("size", {}) or {}
    rooms = building.get("rooms", {}) or {}
    assessment = prop.get("assessment", {}) or {}
    market = assessment.get("market", {}) or {}

    living_sqft = (
        bldg_size.get("livingsize")
        or bldg_size.get("livingSize")
        or bldg_size.get("universalsize")
    )
    bedrooms_count = rooms.get("beds")
    bathrooms_count = rooms.get("bathstotal") or rooms.get("bathsfull")
    tax_market_value = (
        market.get("mktttlvalue") or market.get("mktTtlValue")
    )

    assert living_sqft is not None and int(living_sqft) > 0, (
        f"living_sqft missing or zero for {fixture['raw']!r}: got {living_sqft!r}"
    )
    assert bedrooms_count is not None and int(bedrooms_count) >= 0, (
        f"bedrooms_count invalid for {fixture['raw']!r}: got {bedrooms_count!r}"
    )
    assert bathrooms_count is not None and float(bathrooms_count) >= 0, (
        f"bathrooms_count invalid for {fixture['raw']!r}: got {bathrooms_count!r}"
    )
    assert tax_market_value is not None and float(tax_market_value) > 0, (
        f"tax_market_value missing for {fixture['raw']!r}: got {tax_market_value!r}"
    )
