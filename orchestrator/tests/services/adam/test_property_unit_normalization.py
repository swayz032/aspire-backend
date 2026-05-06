"""Tests for property address unit-normalization in the trades playbook.

Bug repro: transcript 51eb43c3 (2026-05-06) — address with apt/unit number
returns zero results because the raw string was sent to ATTOM unprocessed.

Root cause: execute_property_facts_and_permits used {"address": attom_address}
(legacy single-string path) which passes "apartment 4802" verbatim. ATTOM
cannot resolve apartment-qualified strings when the keyword is not normalised.

Fix: wire parse_us_address() before the ATTOM call so address1 contains the
USPS-normalised unit (e.g. "1575 Paul Russell Rd APT 4802") — the same pattern
already used by landlord.execute_property_facts.

Compliance notes:
  - Law #1: adapter does NOT retry internally; test asserts orchestrator gets failure back.
  - Law #2: every ATTOM call carries correlation_id/suite_id → receipt is generated
            by the provider client; these tests verify the playbook surfaces results,
            not the receipt emission (receipt tests live in tests/audit/).
  - Law #6: tenant_id is carried via suite_id/office_id in PlaybookContext.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(suite_id: str = "suite-test-001", office_id: str = "office-test-001") -> PlaybookContext:
    return PlaybookContext(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id="corr-unit-test",
        tenant_id=suite_id,
    )


def _attom_success(prop_data: dict[str, Any]) -> SimpleNamespace:
    """Mock a successful ATTOM ToolExecutionResult."""
    return SimpleNamespace(
        outcome=Outcome.SUCCESS,
        data={"property": [prop_data]},
        error=None,
        receipt_data={"id": "mock-receipt"},
    )


def _attom_failure(reason: str = "PROVIDER_INTERNAL_ERROR") -> SimpleNamespace:
    """Mock a failed ATTOM ToolExecutionResult."""
    return SimpleNamespace(
        outcome=Outcome.FAILED,
        data=None,
        error=reason,
        receipt_data={"id": "mock-receipt"},
    )


# Minimal ATTOM property detail shape — just enough for normalize_from_attom_detail
# to produce a non-empty PropertyRecord without raising.
_BASE_PROP: dict[str, Any] = {
    "identifier": {"attomId": "ATT-001", "apn": "12-345", "fips": "12073"},
    "address": {"oneLine": "1575 Paul Russell Rd APT 4802, Tallahassee, FL 32301"},
    "summary": {"proptype": "CONDO", "yearbuilt": 2005},
    "building": {
        "summary": {"levels": 4, "quality": "C"},
        "rooms": {"beds": 2, "bathstotal": 2.0},
        "size": {"livingsize": 980},
        "construction": {"frameType": "Masonry"},
        "roof": {"cover": "Flat"},
    },
    "lot": {"lotsize2": 0},
    "assessment": {},
    "owner": {
        "owner1": {"fullname": "SMITH JOHN"},
        "corporateindicator": "N",
        "mailingaddressoneline": "1575 Paul Russell Rd APT 4802, Tallahassee, FL 32301",
        "absenteeownerstatus": "N",
    },
    "sale": {},
    "vintage": {},
    "mortgage": {},
    "area": {},
    "location": {"latitude": "30.4", "longitude": "-84.3"},
}

_SFR_PROP: dict[str, Any] = {
    **_BASE_PROP,
    "identifier": {"attomId": "ATT-SFR-001", "apn": "99-001", "fips": "12073"},
    "address": {"oneLine": "100 Oak St, Tallahassee, FL 32301"},
    "summary": {"proptype": "SFR", "yearbuilt": 1995},
    "building": {
        **_BASE_PROP["building"],  # type: ignore[dict-item]
        "size": {"livingsize": 1800},
    },
}


# ---------------------------------------------------------------------------
# Case A: Full address with apt → normalised APT suffix sent to ATTOM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_a_apt_address_sends_normalised_unit_to_attom() -> None:
    """parse_us_address must normalise 'apartment 4802' → 'APT 4802' in address1,
    and that address1 must be what arrives at execute_attom_detail_mortgage_owner."""
    from aspire_orchestrator.services.adam.playbooks import trades

    captured_payload: dict[str, Any] = {}

    async def _mock_detail(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        captured_payload.update(payload)
        return _attom_success(_BASE_PROP)

    async def _mock_history(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        return _attom_failure("SuccessWithoutResult")

    # Imports are local inside the function, so we patch the provider module directly.
    with (
        patch("aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner", new=AsyncMock(side_effect=_mock_detail)),
        patch("aspire_orchestrator.providers.attom_client.execute_attom_sales_history", new=AsyncMock(side_effect=_mock_history)),
    ):
        query = "1575 Paul Russell Road, apartment 4802, Tallahassee, Florida 32301"
        result = await trades.execute_property_facts_and_permits(query=query, ctx=_ctx())

    # The ATTOM payload must use address1/address2 keys (deterministic path).
    assert "address1" in captured_payload, "Must send address1 key, not raw 'address'"
    assert "APT" in captured_payload["address1"].upper(), (
        f"address1 must include normalised APT suffix, got: {captured_payload['address1']!r}"
    )
    assert "4802" in captured_payload["address1"], "Unit number must be in address1"
    # 'apartment' (full word) must NOT appear in the address sent to ATTOM.
    assert "apartment" not in captured_payload["address1"].lower(), (
        f"Full-word 'apartment' must be normalised away, got: {captured_payload['address1']!r}"
    )
    # Result must include at least one property record.
    assert result.artifact_type == "PropertyFactPack"
    assert len(result.records) >= 1


# ---------------------------------------------------------------------------
# Case B: Full address without apt (single-family home) — no regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_b_sfr_address_no_unit_regression() -> None:
    """Single-family address (no unit) must still resolve correctly."""
    from aspire_orchestrator.services.adam.playbooks import trades

    captured_payload: dict[str, Any] = {}

    async def _mock_detail(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        captured_payload.update(payload)
        return _attom_success(_SFR_PROP)

    async def _mock_history(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        return _attom_failure("SuccessWithoutResult")

    with (
        patch("aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner", new=AsyncMock(side_effect=_mock_detail)),
        patch("aspire_orchestrator.providers.attom_client.execute_attom_sales_history", new=AsyncMock(side_effect=_mock_history)),
    ):
        query = "100 Oak St, Tallahassee, FL 32301"
        result = await trades.execute_property_facts_and_permits(query=query, ctx=_ctx())

    assert "address1" in captured_payload
    # No unit keyword should appear for a plain SFR.
    addr1 = captured_payload["address1"].upper()
    for unit_kw in ("APT", "STE", "UNIT", "BLDG"):
        assert unit_kw not in addr1, f"SFR address1 must not contain {unit_kw}: {addr1!r}"
    assert result.artifact_type == "PropertyFactPack"
    assert len(result.records) >= 1


# ---------------------------------------------------------------------------
# Case C: Malformed apt order ("4802 apartment") — still parses, no crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_c_malformed_apt_order_does_not_crash() -> None:
    """'4802 apartment' (inverted order) is unusual but must not crash the playbook.

    usaddress may either successfully tag it or raise ParseError. In both
    cases the playbook must return a ResearchResponse (never raise).
    """
    from aspire_orchestrator.services.adam.playbooks import trades

    async def _mock_detail(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        return _attom_success(_BASE_PROP)

    async def _mock_history(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        return _attom_failure("SuccessWithoutResult")

    with (
        patch("aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner", new=AsyncMock(side_effect=_mock_detail)),
        patch("aspire_orchestrator.providers.attom_client.execute_attom_sales_history", new=AsyncMock(side_effect=_mock_history)),
    ):
        # "4802 apartment" inverted — usaddress may mis-tag or raise ParseError
        query = "1575 Paul Russell Road, 4802 apartment, Tallahassee, FL 32301"
        result = await trades.execute_property_facts_and_permits(query=query, ctx=_ctx())

    # Must always be a ResearchResponse — never raise.
    assert result is not None
    assert result.artifact_type in ("PropertyFactPack", "needs_more_input", "error"), (
        f"Unexpected artifact_type: {result.artifact_type}"
    )


# ---------------------------------------------------------------------------
# Case D: Regression — exact bug repro transcript 51eb43c3
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_d_regression_51eb43c3_apt_address_returns_record() -> None:
    """Exact repro: '1575 Paul Russell Road, apartment 4802, Tallahassee, Florida 32301'
    must return at least one property record (not empty records=[]).

    ATTOM is mocked to return building-level data with the correct unit address
    in the oneLine field — simulates what the live ATTOM call returns after
    the normalised address1 is sent.
    """
    from aspire_orchestrator.services.adam.playbooks import trades

    call_count: dict[str, int] = {"detail": 0}

    async def _mock_detail(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        call_count["detail"] += 1
        # Verify normalisation happened before we return a result.
        assert "address1" in payload, "Must use address1 key"
        assert "APT" in payload["address1"].upper(), (
            f"Unit must be normalised in address1, got: {payload['address1']!r}"
        )
        return _attom_success(_BASE_PROP)

    async def _mock_history(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        return _attom_failure("SuccessWithoutResult")

    with (
        patch("aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner", new=AsyncMock(side_effect=_mock_detail)),
        patch("aspire_orchestrator.providers.attom_client.execute_attom_sales_history", new=AsyncMock(side_effect=_mock_history)),
    ):
        query = "1575 Paul Russell Road, apartment 4802, Tallahassee, Florida 32301"
        result = await trades.execute_property_facts_and_permits(query=query, ctx=_ctx())

    assert call_count["detail"] == 1, "ATTOM detail must be called exactly once"
    assert result.artifact_type == "PropertyFactPack", (
        f"Expected PropertyFactPack, got {result.artifact_type}. "
        "records may be empty — check ATTOM normalisation."
    )
    assert len(result.records) >= 1, (
        f"Expected at least 1 record, got 0. "
        "Bug still present: address with apt number returns empty results."
    )
    # Confirm address parser is credited.
    assert "address_parser" in result.providers_called


# ---------------------------------------------------------------------------
# Case E: ParseError when address is totally missing city/state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_case_e_parse_error_returns_needs_more_input() -> None:
    """When address lacks city/state, playbook must return needs_more_input
    (not crash, not call ATTOM, not return empty PropertyFactPack)."""
    from aspire_orchestrator.services.adam.playbooks import trades

    attom_call_count: dict[str, int] = {"n": 0}

    async def _mock_detail(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        attom_call_count["n"] += 1
        return _attom_success(_BASE_PROP)

    with patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner",
        new=AsyncMock(side_effect=_mock_detail),
    ):
        # No city, no state — parse_us_address raises ParseError.
        query = "1575 Paul Russell Road apartment 4802"
        result = await trades.execute_property_facts_and_permits(query=query, ctx=_ctx())

    assert attom_call_count["n"] == 0, "ATTOM must NOT be called when address is unparseable"
    assert result.artifact_type == "needs_more_input"
    assert "city" in result.missing_fields or "state" in result.missing_fields


# ---------------------------------------------------------------------------
# Address-parser unit tests (pure function, no I/O)
# ---------------------------------------------------------------------------

def test_parse_us_address_normalises_apartment_keyword() -> None:
    """parse_us_address('... apartment 4802 ...') must produce APT in address1."""
    from aspire_orchestrator.services.adam.address_parser import parse_us_address

    parsed = parse_us_address(
        "1575 Paul Russell Road, apartment 4802, Tallahassee, Florida 32301"
    )
    assert "APT" in parsed.address1.upper()
    assert "4802" in parsed.address1
    assert "apartment" not in parsed.address1.lower()


def test_parse_us_address_sfr_no_unit() -> None:
    """Single-family address without unit must parse cleanly."""
    from aspire_orchestrator.services.adam.address_parser import parse_us_address

    parsed = parse_us_address("100 Oak St, Tallahassee, FL 32301")
    assert parsed.address1 == "100 Oak St"
    assert "Tallahassee" in parsed.address2
    assert "FL" in parsed.address2


def test_parse_us_address_suite_normalises_to_ste() -> None:
    """'Suite 200' must normalise to 'STE 200' in address1."""
    from aspire_orchestrator.services.adam.address_parser import parse_us_address

    parsed = parse_us_address("200 Main St, suite 200, Austin, TX 78701")
    assert "STE" in parsed.address1.upper()
    assert "200" in parsed.address1


# ---------------------------------------------------------------------------
# P0-1: Receipt emission tests (Law #2 — 100% exit-path coverage)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipt_emitted_on_parse_error_exit() -> None:
    """ParseError path MUST emit a receipt with outcome=FAILED, reason=address_parse_error.

    Law #2: every exit path of execute_property_facts_and_permits must produce
    an immutable receipt so the audit trail is complete regardless of outcome.
    """
    from aspire_orchestrator.services.adam.playbooks import trades

    receipts_emitted: list[list[dict[str, Any]]] = []

    def _capture_store(receipts: list[dict[str, Any]]) -> None:
        receipts_emitted.extend(receipts)

    with patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        side_effect=_capture_store,
    ):
        # Address with no city/state triggers ParseError.
        result = await trades.execute_property_facts_and_permits(
            query="1575 Paul Russell Road apartment 4802",
            ctx=_ctx(),
        )

    assert result.artifact_type == "needs_more_input"
    assert len(receipts_emitted) >= 1, "No receipt emitted on ParseError exit"
    r = receipts_emitted[0]
    assert r["outcome"] == "failed", f"Expected outcome=failed, got {r['outcome']!r}"
    assert r["reason_code"] == "address_parse_error", f"Wrong reason_code: {r['reason_code']!r}"
    # Law #9: raw address must never appear in receipt logs.
    assert r.get("redacted_inputs") is not None, "redacted_inputs must be set"
    raw_street = "1575"
    assert raw_street not in str(r.get("redacted_inputs", "")), (
        "Street number must be masked in redacted_inputs (Law #9)"
    )


@pytest.mark.asyncio
async def test_receipt_emitted_on_attom_error_exit() -> None:
    """ATTOM error path MUST emit a receipt with outcome=FAILED, reason=attom_unavailable."""
    from aspire_orchestrator.services.adam.playbooks import trades

    receipts_emitted: list[dict[str, Any]] = []

    def _capture_store(receipts: list[dict[str, Any]]) -> None:
        receipts_emitted.extend(receipts)

    async def _mock_detail_fail(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        return _attom_failure("PROVIDER_INTERNAL_ERROR")

    with (
        patch("aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner", new=AsyncMock(side_effect=_mock_detail_fail)),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=_capture_store),
    ):
        result = await trades.execute_property_facts_and_permits(
            query="1575 Paul Russell Road, apartment 4802, Tallahassee, FL 32301",
            ctx=_ctx(),
        )

    assert result.artifact_type == "error"
    # At least 1 receipt from the playbook-level rollup.
    playbook_receipts = [r for r in receipts_emitted if r.get("action_type", "").endswith("PROPERTY_FACTS_AND_PERMITS")]
    assert len(playbook_receipts) >= 1, "No playbook-level receipt emitted on ATTOM error"
    r = playbook_receipts[0]
    assert r["outcome"] == "failed"
    assert r["reason_code"] == "attom_unavailable"
    assert r.get("redacted_inputs") is not None


@pytest.mark.asyncio
async def test_receipt_emitted_on_success_exit() -> None:
    """Success path MUST emit a receipt with outcome=SUCCEEDED and risk_tier=yellow."""
    from aspire_orchestrator.services.adam.playbooks import trades

    receipts_emitted: list[dict[str, Any]] = []

    def _capture_store(receipts: list[dict[str, Any]]) -> None:
        receipts_emitted.extend(receipts)

    async def _mock_detail(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        return _attom_success(_BASE_PROP)

    async def _mock_history(*, payload: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        return _attom_failure("SuccessWithoutResult")

    with (
        patch("aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner", new=AsyncMock(side_effect=_mock_detail)),
        patch("aspire_orchestrator.providers.attom_client.execute_attom_sales_history", new=AsyncMock(side_effect=_mock_history)),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=_capture_store),
    ):
        result = await trades.execute_property_facts_and_permits(
            query="1575 Paul Russell Road, apartment 4802, Tallahassee, FL 32301",
            ctx=_ctx(),
        )

    assert result.artifact_type == "PropertyFactPack"
    playbook_receipts = [r for r in receipts_emitted if r.get("action_type", "").endswith("PROPERTY_FACTS_AND_PERMITS")]
    assert len(playbook_receipts) >= 1, "No playbook-level receipt emitted on success"
    r = playbook_receipts[0]
    assert r["outcome"] == "succeeded"
    assert r["reason_code"] == "property_facts_success"
    # YELLOW risk tier for mortgage/ownership data (auditor sign-off 2026-05-06).
    assert r.get("risk_tier") == "yellow", (
        f"PropertyFactPack must be YELLOW (mortgage/ownership data), got {r.get('risk_tier')!r}"
    )
    assert r.get("redacted_outputs", {}).get("record_count", -1) >= 1


# ---------------------------------------------------------------------------
# _redact_address unit tests (Law #9 — PII scrubbing before receipt storage)
# ---------------------------------------------------------------------------

def test_redact_address_strips_unit_and_masks_street_number() -> None:
    """Unit identifiers and long street numbers must be scrubbed."""
    from aspire_orchestrator.services.adam.playbooks.trades import _redact_address

    result = _redact_address("1575 Paul Russell Road, apartment 4802, Tallahassee, FL 32301")
    assert "apartment" not in result.lower()
    assert "4802" not in result
    assert "1575" not in result   # masked to XXX
    assert "XXX" in result
    assert "Paul Russell Road" in result


def test_redact_address_apt_abbreviation() -> None:
    """'apt' abbreviation must be stripped."""
    from aspire_orchestrator.services.adam.playbooks.trades import _redact_address

    result = _redact_address("200 Oak St, apt 12, Austin TX 78701")
    assert "apt" not in result.lower()
    assert "12" not in result or "apt" not in result.lower()


def test_redact_address_hash_prefix() -> None:
    """'#202' style unit must be stripped."""
    from aspire_orchestrator.services.adam.playbooks.trades import _redact_address

    result = _redact_address("300 Main Ave #202, Dallas TX 75201")
    assert "#202" not in result
    assert "#" not in result or "202" not in result


def test_redact_address_short_street_number_not_masked() -> None:
    """Street numbers with fewer than 4 digits are NOT masked (less identifying)."""
    from aspire_orchestrator.services.adam.playbooks.trades import _redact_address

    result = _redact_address("42 Elm St, Boston, MA 02101")
    # Short numbers (< 4 digits) should remain — masking them adds noise without
    # meaningful PII reduction for common low-number addresses.
    assert "XXX" not in result
