"""BLS OEWS Provider Client — Occupational Employment and Wage Statistics.

Provider: U.S. Bureau of Labor Statistics (https://api.bls.gov)
Auth: Optional API key (ASPIRE_BLS_API_KEY). Without key: 25 req/day, 25 series/request.
      With key (v2): 500 req/day, 50 series/request.
Risk tier: GREEN (read-only .gov data, free)
Idempotency: N/A (read-only)

API endpoint: POST https://api.bls.gov/publicAPI/v2/timeseries/data/
Content-Type: application/json
Body: {"seriesid": ["OEUM{area}{soc}{measure}"], "registrationkey": "..."}

OEWS Series ID format: OEU{S|M}{area_code}{soc_code_nodash}{measure_code}
  - S = state, M = MSA/metro area
  - area_code: 7-digit area FIPS (MSA code padded) or state code
  - soc_code_nodash: 6-digit SOC without dash (e.g. 472111 for 47-2111)
  - measure_code: 01=employment, 03=mean hourly, 04=mean annual, 08=median hourly,
                  09=median annual, 11=10th pct hourly, 12=25th pct hourly,
                  13=75th pct hourly, 14=90th pct hourly

SOC codes locked per plan:
  electrician    → 47-2111
  plumber        → 47-2152
  drywaller      → 47-2081
  painter        → 47-2141
  hvac           → 49-9021

Data vintage: BLS OEWS May 2024 (most recent annual release). API returns
  latest available data when no year range is specified.

Law compliance:
  Law #1: Returns raw wage data; Adam/orchestrator decides how to present.
  Law #2: Every call produces a receipt.
  Law #3: Fails closed if soc_code is unrecognised or API returns no data.
  Law #9: No PII in OEWS data — only public wage statistics.

Tools:
  - bls_oews.get_wage: Retrieve occupational wage data by SOC code + geography
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locked SOC code registry (plan §3.2)
# ---------------------------------------------------------------------------
_SOC_REGISTRY: dict[str, str] = {
    "electrician": "47-2111",
    "electric": "47-2111",
    "electrical": "47-2111",
    "plumber": "47-2152",
    "plumbing": "47-2152",
    "drywaller": "47-2081",
    "drywall": "47-2081",
    "painter": "47-2141",
    "painting": "47-2141",
    "hvac": "49-9021",
    "air conditioning": "49-9021",
    "heating": "49-9021",
    "refrigeration": "49-9021",
}

# ---------------------------------------------------------------------------
# Measure code → field name mapping for response normalisation
# ---------------------------------------------------------------------------
_MEASURE_CODES: dict[str, str] = {
    "03": "mean_hourly",
    "08": "median_hourly",
    "04": "mean_annual",
    "09": "median_annual",
    "11": "pct_10_hourly",
    "12": "pct_25_hourly",
    "13": "pct_75_hourly",
    "14": "pct_90_hourly",
    "01": "employment",
}

# Measures to request in a single call (minimise API round trips)
_DEFAULT_MEASURES = ["03", "08", "11", "12", "13", "14", "01"]

# Area type codes
_AREA_TYPE_NATIONAL = "N"
_AREA_TYPE_MSA = "M"


@dataclass
class WageResult:
    """Normalised wage data for a single SOC + geography combination."""

    soc_code: str
    area_code: str
    area_type: str  # "national", "state", "msa"
    mean_hourly: float | None
    median_hourly: float | None
    pct_10_hourly: float | None
    pct_25_hourly: float | None
    pct_75_hourly: float | None
    pct_90_hourly: float | None
    employment: int | None
    year: str | None
    period: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "soc_code": self.soc_code,
            "area_code": self.area_code,
            "area_type": self.area_type,
            "mean_hourly": self.mean_hourly,
            "median_hourly": self.median_hourly,
            "pct_10_hourly": self.pct_10_hourly,
            "pct_25_hourly": self.pct_25_hourly,
            "pct_75_hourly": self.pct_75_hourly,
            "pct_90_hourly": self.pct_90_hourly,
            "employment": self.employment,
            "year": self.year,
            "period": self.period,
        }


def resolve_soc_code(trade: str) -> str | None:
    """Map a trade name to a locked SOC code. Returns None if unrecognised."""
    key = trade.strip().lower()
    return _SOC_REGISTRY.get(key)


def _build_series_id(area_type: str, area_code: str, soc_code: str, measure_code: str) -> str:
    """Build a BLS OEWS series ID.

    Format (25 chars):
      OEU + {areatype_1}{area_7} + {industry_6} + {soc_nodash_6} + {measure_2}

    area_type values:
      "N" = National   (area_code ignored, uses 0000000)
      "M" = MSA        (area_code = MSA numeric padded to 7 digits)
      "S" = State      (area_code = state FIPS padded to 7 digits)

    industry_6 = "000000" (cross-industry, all industries combined)

    Example — national electrician median hourly:
      OEU + N0000000 + 000000 + 472111 + 08
      = OEUN000000000000047211108
    """
    soc_nodash = soc_code.replace("-", "")
    if area_type == "N":
        area_segment = "N0000000"
    elif area_type == "M":
        area_segment = "M" + area_code.zfill(7)
    elif area_type == "S":
        area_segment = "S" + area_code.zfill(7)
    else:
        area_segment = area_type + area_code.zfill(7)
    return f"OEU{area_segment}000000{soc_nodash}{measure_code}"


def _parse_value(raw: str | None) -> float | None:
    """Parse a BLS value string, handling dashes and asterisks (suppressed data)."""
    if not raw:
        return None
    raw = raw.strip()
    if raw in ("", "-", "*", "**", "#", "N/A"):
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


class BLSOEWSClient(BaseProviderClient):
    """BLS Public Data API client — OEWS occupational wage series."""

    provider_id = "bls_oews"
    base_url = "https://api.bls.gov"
    timeout_seconds = 8.0  # BLS API can be slow under load
    max_retries = 0  # Adapter never retries; orchestrator owns retry logic (Law #1)
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        # BLS v2 accepts registrationkey in the JSON body (not headers).
        return {"Content-Type": "application/json"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 400:
            return InternalErrorCode.INPUT_INVALID_FORMAT
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if status_code in (500, 503):
            return InternalErrorCode.PROVIDER_INTERNAL_ERROR
        return super()._parse_error(status_code, body)


_client: BLSOEWSClient | None = None


def _get_client() -> BLSOEWSClient:
    global _client
    if _client is None:
        _client = BLSOEWSClient()
    return _client


def _get_api_key() -> str | None:
    """Return BLS API key if configured. None = unauthenticated (25 req/day)."""
    return os.environ.get("ASPIRE_BLS_API_KEY") or None


def _extract_wage_data(
    series_data: list[dict[str, Any]],
    soc_code: str,
    area_code: str,
    area_type: str,
) -> WageResult:
    """Extract wage fields from multiple BLS series responses into a WageResult."""
    values: dict[str, float | None] = {}
    year: str | None = None
    period: str | None = None

    for series in series_data:
        series_id: str = series.get("seriesID", "")
        data_points: list[dict[str, Any]] = series.get("data", [])
        if not data_points:
            continue

        # BLS returns most-recent first; use data_points[0]
        latest = data_points[0]
        if year is None:
            year = latest.get("year")
            period = latest.get("period")

        # Extract measure code from series ID (last 2 chars)
        measure_code = series_id[-2:] if len(series_id) >= 2 else ""
        field_name = _MEASURE_CODES.get(measure_code)
        if not field_name:
            continue

        raw_val = latest.get("value")
        values[field_name] = _parse_value(raw_val)

    employment_raw = values.get("employment")
    employment: int | None = int(employment_raw) if employment_raw is not None else None

    return WageResult(
        soc_code=soc_code,
        area_code=area_code,
        area_type=area_type,
        mean_hourly=values.get("mean_hourly"),
        median_hourly=values.get("median_hourly"),
        pct_10_hourly=values.get("pct_10_hourly"),
        pct_25_hourly=values.get("pct_25_hourly"),
        pct_75_hourly=values.get("pct_75_hourly"),
        pct_90_hourly=values.get("pct_90_hourly"),
        employment=employment,
        year=year,
        period=period,
    )


async def execute_bls_oews_get_wage(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
    timeout: float = 8.0,
) -> ToolExecutionResult:
    """Execute bls_oews.get_wage — retrieve OEWS wage data for a SOC code + geography.

    Required payload:
      - soc_code: str — BLS SOC code (e.g. "47-2111") OR
      - trade: str    — Trade name resolved to SOC via _SOC_REGISTRY

    Optional payload:
      - msa_code: str | None — MSA numeric code (e.g. "19460" for Austin TX).
                               Padded to 7 digits internally.
      - state: str | None    — 2-letter state abbreviation (NOT yet implemented;
                               MSA is preferred. Falls back to national if omitted.)

    If neither msa_code nor state is provided, returns national estimates.
    """
    client = _get_client()
    tool_id = "bls_oews.get_wage"

    # Resolve SOC code
    soc_code = str(payload.get("soc_code") or "").strip()
    trade = str(payload.get("trade") or "").strip()

    if not soc_code and trade:
        resolved = resolve_soc_code(trade)
        if resolved is None:
            receipt = client.make_receipt_data(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                tool_id=tool_id,
                risk_tier=risk_tier,
                outcome=Outcome.FAILED,
                reason_code="INPUT_MISSING_REQUIRED",
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
            )
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id=tool_id,
                error=f"Unrecognised trade: {trade!r}. Use soc_code directly or one of: {sorted(_SOC_REGISTRY.keys())}",
                receipt_data=receipt,
            )
        soc_code = resolved

    if not soc_code:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: soc_code or trade",
            receipt_data=receipt,
        )

    # Determine area code and type
    msa_code = str(payload.get("msa_code") or "").strip()
    if msa_code:
        bls_area_type = _AREA_TYPE_MSA
        area_code = msa_code.zfill(7)
        area_type = "msa"
    else:
        bls_area_type = _AREA_TYPE_NATIONAL
        area_code = "0000000"
        area_type = "national"

    # Build series IDs for all requested measures
    series_ids = [
        _build_series_id(bls_area_type, area_code, soc_code, measure)
        for measure in _DEFAULT_MEASURES
    ]

    # Build POST body
    api_key = _get_api_key()
    body: dict[str, Any] = {"seriesid": series_ids}
    if api_key:
        body["registrationkey"] = api_key

    request = ProviderRequest(
        method="POST",
        path="/publicAPI/v2/timeseries/data/",
        body=body,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )

    try:
        response = await asyncio.wait_for(
            client._request(request), timeout=timeout
        )
    except asyncio.TimeoutError:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.NETWORK_TIMEOUT.value,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        receipt["redacted_outputs"] = {
            "soc_code": soc_code,
            "area_code": area_code,
            "area_type": area_type,
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"BLS OEWS API timeout after {timeout}s",
            receipt_data=receipt,
        )

    if not response.success:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=response.error_code.value if response.error_code else "FAILED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            provider_response=response,
        )
        receipt["redacted_outputs"] = {
            "soc_code": soc_code,
            "area_code": area_code,
            "area_type": area_type,
            "http_status": response.status_code,
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"BLS OEWS error: HTTP {response.status_code}",
            receipt_data=receipt,
        )

    # BLS API v2 wraps data under response.body["Results"]["series"]
    bls_status = str(response.body.get("status") or "").upper()
    if bls_status == "REQUEST_FAILED":
        messages = response.body.get("message") or []
        msg_str = "; ".join(str(m) for m in messages) if messages else "BLS API request failed"
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="PROVIDER_INTERNAL_ERROR",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            provider_response=response,
        )
        receipt["redacted_outputs"] = {
            "soc_code": soc_code,
            "area_code": area_code,
            "area_type": area_type,
            "bls_status": bls_status,
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"BLS API request failed: {msg_str}",
            receipt_data=receipt,
        )

    results_obj = response.body.get("Results") or {}
    series_list: list[dict[str, Any]] = results_obj.get("series") or []

    if not series_list:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="RESOURCE_NOT_FOUND",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            provider_response=response,
        )
        receipt["redacted_outputs"] = {
            "soc_code": soc_code,
            "area_code": area_code,
            "area_type": area_type,
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"No BLS OEWS data found for SOC {soc_code} in area {area_code}",
            receipt_data=receipt,
        )

    wage_result = _extract_wage_data(
        series_data=series_list,
        soc_code=soc_code,
        area_code=area_code,
        area_type=area_type,
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id=tool_id,
        risk_tier=risk_tier,
        outcome=Outcome.SUCCESS,
        reason_code="EXECUTED",
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )
    receipt["redacted_outputs"] = {
        "soc_code": soc_code,
        "area_code": area_code,
        "area_type": area_type,
        "median_hourly": wage_result.median_hourly,
        "year": wage_result.year,
    }

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=wage_result.to_dict(),
        receipt_data=receipt,
    )
