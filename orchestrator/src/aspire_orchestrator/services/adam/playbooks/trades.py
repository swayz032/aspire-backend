"""TRADES Playbooks — 6 research playbooks for trades ICP.

Segments: plumbers, HVAC, electricians, roofers, painters, GCs, landscapers
Playbooks: Property Facts & Permits, Estimate Research, Tool/Material Price Check,
           Competitor Pricing Scan, Subcontractor Scout, Territory Opportunity Scan
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import random as _random
import re
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.verifier import verify_records

logger = logging.getLogger(__name__)

# Round 7 A.2 — HD-too-far threshold (miles). Above this distance the prompt
# offers Lowe's/Ace fallback. Tuneable without code rebuild.
HD_TOO_FAR_MILES = 25.0

# Round 7 A.2 — SerpApi shopping retry policy. Voice path budget is ≤ 4.5s P95;
# we reserve ~1.5s of total slack for these retries. Two retries with
# exponential-backoff-with-jitter (250ms, 500ms base + 0-100ms jitter), then
# graceful degrade to empty.
_SHOPPING_RETRY_MAX_ATTEMPTS = 2
_SHOPPING_RETRY_BASE_MS = (250, 500)


def _emit_playbook_receipt(
    *,
    ctx: PlaybookContext,
    outcome_status: str,                # SUCCEEDED | FAILED | DENIED
    reason_code: str,
    playbook_name: str,
    summary: dict[str, Any] | None = None,
    redacted_inputs: dict[str, Any] | None = None,
    redacted_outputs: dict[str, Any] | None = None,
    risk_tier: str = "green",
) -> None:
    """Emit a playbook-rollup receipt for Adam (Law #2 — 100% coverage).

    Provider clients already emit one receipt per HTTP call. This receipt is
    the playbook-level rollup so every Adam outcome (success, MISSING_TASK,
    shopping_429, hd_too_far, no_stock, multi_store_success) has at least
    one receipt with actor_type=WORKER and the correct status. Best-effort:
    receipt-store failures are logged and swallowed so receipt persistence
    NEVER blocks the user-facing response (Law #2 + reliability balance).

    Parameters
    ----------
    redacted_inputs:
        PII-scrubbed representation of what went INTO this playbook call.
        Never pass raw addresses, owner names, or financial data.
    redacted_outputs:
        Summary of what came OUT (record counts, artifact_type, status).
        Supersedes the legacy ``summary`` kwarg when both are provided.
    risk_tier:
        Override the default ``green`` tier when the playbook touches
        sensitive data (e.g. YELLOW for mortgage/ownership records).
    """
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts

        # Outcome string at the receipt-store layer is lowercased by status_map.
        outcome_lower = outcome_status.lower()
        # redacted_outputs wins over legacy summary kwarg so existing callers
        # that only pass summary= continue to work unchanged.
        effective_outputs = redacted_outputs if redacted_outputs is not None else summary
        receipt: dict[str, Any] = {
            "id": str(_uuid.uuid4()),
            "correlation_id": ctx.correlation_id or "",
            "suite_id": ctx.suite_id or "",
            "office_id": ctx.office_id or "",
            "tenant_id": ctx.tenant_id or ctx.suite_id or "",
            "actor_type": "WORKER",
            "actor_id": "adam",
            "action_type": f"adam.playbook.{playbook_name}",
            "risk_tier": risk_tier,
            "tool_used": "adam_playbook",
            "outcome": outcome_lower,
            "reason_code": reason_code,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "receipt_type": "agent_playbook",
            "redacted_inputs": redacted_inputs,
            "redacted_outputs": effective_outputs,
            "capability_token_id": ctx.capability_token_id,
            "capability_token_hash": ctx.capability_token_hash,
        }
        # Receipt hash for chain integrity. Sort keys so the hash is deterministic.
        try:
            payload = _json.dumps(receipt, sort_keys=True, default=str)
            receipt["receipt_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        except Exception:
            receipt["receipt_hash"] = ""
        store_receipts([receipt])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Adam playbook receipt emission failed (outcome=%s reason=%s): %s",
            outcome_status, reason_code, exc,
        )


_UNIT_KEYWORD_RE = re.compile(
    r'(?:\b(?:apartment|apt|unit|suite|ste)\s*\S+|#\s*\S+)',
    re.IGNORECASE,
)
_LEADING_DIGITS_RE = re.compile(r'^\d{4,}')


def _redact_address(raw: str) -> str:
    """Return a PII-scrubbed version of a raw address string (Law #9).

    Strips occupant/unit identifiers and masks long leading street numbers
    so owner-identifiable data is not persisted in receipt logs.

    Examples:
        "1575 Paul Russell Road, apartment 4802, Tallahassee, FL 32301"
        → "XXX Paul Russell Road, Tallahassee, FL 32301"

        "200 Main St, Suite 400, Austin TX 78701"
        → "XXX Main St, Austin TX 78701"
    """
    # Remove unit/apt qualifiers ("apartment 4802", "apt 4802", "#4802", etc.)
    redacted = _UNIT_KEYWORD_RE.sub('', raw)
    # Collapse double commas/spaces left by removal
    redacted = re.sub(r',\s*,', ',', redacted)
    redacted = re.sub(r'\s{2,}', ' ', redacted).strip().strip(',').strip()
    # Mask leading street number when it is ≥4 digits (unit-level identifiable)
    parts = redacted.split(' ', 1)
    if parts and _LEADING_DIGITS_RE.match(parts[0]):
        parts[0] = 'XXX'
        redacted = ' '.join(parts)
    return redacted


def _extract_address_from_query(query: str) -> str:
    """Extract address from a natural language query for ATTOM."""
    import re
    # Strict pattern (state abbreviation or full state name)
    match = re.search(
        r'(\d+\s+[\w\s]+(?:St|Ave|Rd|Blvd|Dr|Ln|Ct|Way|Pl|Cir|Pkwy|Hwy|Ter)\.?'
        r'(?:\s*,\s*[\w\s]+,?\s*(?:[A-Z]{2}|[A-Za-z]{4,})\s*,?\s*\d{5}(?:-\d{4})?))',
        query, re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    prefixes = [
        "pull property facts for", "pull property details for", "pull property profile for",
        "find property facts for", "find property details for", "find property profile for",
        "property facts for", "property details for", "property profile for",
        "pull the square footage and permit context for",
        "pull", "get", "show me", "find", "look up",
    ]
    remaining = query.strip()
    while remaining:
        q_lower = remaining.lower().strip()
        consumed = False
        for prefix in sorted(prefixes, key=len, reverse=True):
            if q_lower.startswith(prefix):
                remaining = remaining[len(prefix):].strip(" .,:;-")
                consumed = True
                break
        if not consumed:
            break
    marker = "additional details:"
    rem_lower = remaining.lower()
    if marker in rem_lower:
        idx = rem_lower.rfind(marker)
        tail = remaining[idx + len(marker):].strip(" .,:;-")
        if tail:
            return tail
    if remaining and remaining != query:
        return remaining
    lower_query = query.lower()
    if marker in lower_query:
        idx = lower_query.rfind(marker)
        tail = query[idx + len(marker):].strip(" .,:;-")
        if tail:
            return tail
    # Loose fallback for wrapped inputs like:
    # "property lookup. Additional details: 4863 Price Street, Forest Park, Georgia, 30297"
    loose = re.search(
        r'(\d+\s+[\w\s]+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Way|Pl|Place|Cir|Circle|Pkwy|Parkway|Hwy|Highway|Ter|Terrace)\b[^,\n]*'
        r'(?:,\s*[\w\s]+){0,2}\s*,?\s*(?:[A-Z]{2}|[A-Za-z]{4,})\s*,?\s*\d{5}(?:-\d{4})?)',
        query,
        re.IGNORECASE,
    )
    if loose:
        return loose.group(1).strip()
    return query


async def execute_property_facts_and_permits(
    query: str, ctx: PlaybookContext, address: str = "",
) -> ResearchResponse:
    """PROPERTY_FACTS_AND_PERMITS — Resolve property context for quoting.

    Calls ATTOM (facts + sales history) AND Apify Zillow (photos) in
    parallel. Photos are merged into the first record under a `photos` key
    with `lane` (interior/exterior/roof/uncategorized) classification, ready
    for the Aspire-Desktop Visuals tab to consume.
    """
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_detail_mortgage_owner,
        execute_attom_sales_history,
    )
    from aspire_orchestrator.providers.apify_zillow_client import (
        execute_apify_zillow_photos,
    )
    from aspire_orchestrator.services.adam.address_parser import (
        ParseError,
        parse_us_address,
    )
    from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
        normalize_from_attom_detail,
        normalize_from_attom_sales_history,
    )

    logger.info("Executing PROPERTY_FACTS_AND_PERMITS for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Step 1: Deterministic address parse (usaddress CRF tagger).
    # parse_us_address normalises "apartment 4802" → "APT 4802" and emits
    # address1/address2 in ATTOM /expandedprofile format. Sending address1
    # WITH the normalised unit (e.g. "1575 Paul Russell Rd APT 4802") lets
    # ATTOM resolve to the unit-level parcel. ATTOM rejects the separate
    # `unitnumber` query param as invalid (verified 2026-05-04).
    raw_address = address or _extract_address_from_query(query)
    try:
        parsed = parse_us_address(raw_address)
        attom_payload: dict[str, str] = {
            "address1": parsed.address1,
            "address2": parsed.address2,
            "address": f"{parsed.address1}, {parsed.address2}",
        }
        normalized_address = attom_payload["address"]
        # Cache parsed unit info on the receipt context so show_cards can
        # render unit-specific fields without re-parsing downstream.
        _unit_type: str = parsed.components.get("OccupancyType", "")
        _unit_num: str = parsed.components.get("OccupancyIdentifier", "")
        logger.info(
            "PROPERTY_FACTS_AND_PERMITS: parsed address1=%r address2=%r unit=%r%r",
            parsed.address1, parsed.address2, _unit_type, _unit_num,
        )
        providers_called.append("address_parser")
    except ParseError as exc:
        logger.info(
            "PROPERTY_FACTS_AND_PERMITS: address parse failed — %s (raw=%r)",
            exc, raw_address[:80],
        )
        # P0-1 (Law #2): receipt for ParseError exit path.
        _emit_playbook_receipt(
            ctx=ctx,
            outcome_status="FAILED",
            reason_code="address_parse_error",
            playbook_name="PROPERTY_FACTS_AND_PERMITS",
            risk_tier="green",
            redacted_inputs={"raw_address": _redact_address(raw_address)},
            redacted_outputs={"error": str(exc)[:200]},
        )
        return ResearchResponse(
            artifact_type="needs_more_input",
            summary=(
                "I need a complete street address with city and state to look that up — "
                "what's the full address including city and state?"
            ),
            records=[],
            sources=[],
            freshness={"mode": "live", "provider": "address_parser"},
            confidence={"status": "needs_input", "score": 0.0},
            missing_fields=["city", "state"],
            next_queries=["Provide street, city, state (and ZIP if available)"],
            segment="trades",
            intent="property_fact",
            playbook="PROPERTY_FACTS_AND_PERMITS",
            providers_called=providers_called,
            extra={"parse_error": str(exc), "raw_query": query},
        )

    # 1. ATTOM property detail + Apify Zillow photos in PARALLEL.
    # Both keyed only by the address; running serially would cost the user
    # 8s (ATTOM) + 10–15s (Apify cold start). Parallelizing keeps the user
    # under the perceived-instant ceiling.
    detail_result, apify_result = await asyncio.gather(
        execute_attom_detail_mortgage_owner(
            payload=attom_payload,
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
            capability_token_id=ctx.capability_token_id,
            capability_token_hash=ctx.capability_token_hash,
        ),
        execute_apify_zillow_photos(
            payload={"address": normalized_address},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
            capability_token_id=ctx.capability_token_id,
            capability_token_hash=ctx.capability_token_hash,
        ),
        return_exceptions=False,
    )
    providers_called.append("attom")
    providers_called.append("apify_zillow")

    if detail_result.outcome.value == "success" and detail_result.data:
        prop = normalize_from_attom_detail(detail_result.data)
        record_dict = prop.to_dict()

        # Merge Apify Zillow photos into the record. Failure is non-fatal —
        # property facts still flow through; only the photo lanes go empty.
        try:
            apify_outcome = apify_result.outcome.value if apify_result else None
            apify_data = apify_result.data if apify_result else None
            if apify_outcome == "success" and isinstance(apify_data, dict):
                photos_raw = apify_data.get("photos") or []
                if isinstance(photos_raw, list) and photos_raw:
                    record_dict["photos"] = [
                        {
                            "url": (p.get("url") or "").strip(),
                            "caption": p.get("caption"),
                            "lane": (p.get("lane") or "uncategorized").strip().lower(),
                        }
                        for p in photos_raw
                        if isinstance(p, dict) and p.get("url")
                    ]
                    listing_url = apify_data.get("listing_url")
                    if listing_url:
                        record_dict["zillow_listing_url"] = listing_url
                    logger.info(
                        "Apify Zillow merged %d photos into property record",
                        len(record_dict["photos"]),
                    )
                else:
                    logger.info(
                        "Apify Zillow returned 0 photos for %s",
                        normalized_address[:60],
                    )
            else:
                logger.warning(
                    "Apify Zillow unavailable (outcome=%s) — continuing photos-less",
                    apify_outcome,
                )
                providers_called.append("apify_zillow_failed")
        except Exception as merge_err:  # noqa: BLE001 — merge must never break facts
            logger.warning("Apify Zillow merge failed (non-fatal): %s", str(merge_err)[:160])

        records.append(record_dict)
        sources.extend(prop.sources)
    else:
        # F-HIGH-8 + F-MED-1: ATTOM 500/timeout/network used to swallow the
        # outcome and emit an empty PropertyFactPack. Surface a structured
        # error response so Ava can tell the user "the property service is
        # unavailable" instead of "I found nothing".
        logger.warning(
            "ATTOM property detail unavailable for %s — outcome=%s",
            normalized_address[:60], detail_result.outcome.value,
        )
        # P0-1 (Law #2): receipt for ATTOM error exit path. address1/address2
        # from the parsed struct do not contain owner/occupant PII — safe to log.
        _emit_playbook_receipt(
            ctx=ctx,
            outcome_status="FAILED",
            reason_code="attom_unavailable",
            playbook_name="PROPERTY_FACTS_AND_PERMITS",
            risk_tier="green",
            redacted_inputs={
                "address1": parsed.address1,
                "address2": parsed.address2,
            },
            redacted_outputs={
                "detail_outcome": str(detail_result.outcome.value),
            },
        )
        return ResearchResponse(
            artifact_type="error",
            summary="The property records service is temporarily unavailable. Try again in 30 seconds.",
            records=[],
            sources=[],
            freshness={"mode": "live"},
            confidence={"status": "unverified", "score": 0.0},
            missing_fields=["property"],
            next_queries=["Try again shortly", "Pull property facts again in a moment"],
            segment="trades",
            intent="property_fact",
            playbook="PROPERTY_FACTS_AND_PERMITS",
            providers_called=providers_called,
            extra={
                "reason": "attom_unavailable",
                "suggested_retry_after_seconds": 30,
                "provider_outcome": detail_result.outcome.value,
            },
        )

    # 2. Stage 1.5 — parallel fan-out: ATTOM sales history + Apify Zillow photos.
    # Both are best-effort and run AFTER the ATTOM detail succeeds (above).
    # asyncio.gather(return_exceptions=True) ensures one provider's failure
    # does not kill the other (Law #3 — fail-closed for the playbook overall,
    # graceful degradation for supplementary data).
    import asyncio as _asyncio
    from aspire_orchestrator.providers.apify_zillow_client import (
        execute_apify_zillow_photos,
    )
    # Note: photo normalization happens inside execute_apify_zillow_photos.
    # The result.data already contains the categorized photos[] list.

    history_task = execute_attom_sales_history(
        payload=attom_payload,
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
        capability_token_id=ctx.capability_token_id,
        capability_token_hash=ctx.capability_token_hash,
    )
    photos_task = execute_apify_zillow_photos(
        payload={"address": normalized_address},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
        capability_token_id=ctx.capability_token_id,
        capability_token_hash=ctx.capability_token_hash,
    )

    history_result, photos_result = await _asyncio.gather(
        history_task, photos_task, return_exceptions=True,
    )

    # ATTOM sales history (supplementary).
    if isinstance(history_result, Exception):
        logger.info(
            "ATTOM sales history raised %s — continuing without history",
            type(history_result).__name__,
        )
        providers_called.append("attom_sales_history_failed")
    elif history_result.outcome.value == "success" and history_result.data:
        sales = normalize_from_attom_sales_history(history_result.data)
        if sales and records:
            records[0]["sale_history"] = [
                {"date": s.date, "amount": s.amount, "trans_type": s.trans_type,
                 "buyer": s.buyer, "seller": s.seller}
                for s in sales
            ]
    elif history_result.outcome.value != "success":
        # F-MED-1: log + provider attribution. The detail path already gave
        # us core property data; sales history is supplementary, so we note
        # the failure in providers_called rather than returning an error.
        logger.info(
            "ATTOM sales history unavailable (outcome=%s) — continuing without history",
            history_result.outcome.value,
        )
        providers_called.append("attom_sales_history_failed")

    # Apify Zillow photos (supplementary — Visuals tab interior/exterior/roof).
    # Photos are attached to records[0] so the desktop adamResearchClient can
    # surface them as records[0].photos in the ResearchResponse contract.
    photos: list[dict[str, Any]] = []
    if isinstance(photos_result, Exception):
        logger.info(
            "Apify Zillow scrape raised %s — continuing without photos",
            type(photos_result).__name__,
        )
        providers_called.append("apify_zillow_failed")
    elif photos_result.outcome.value == "success" and photos_result.data:
        photos = list(photos_result.data.get("photos") or [])
        if photos:
            providers_called.append("apify_zillow")
        else:
            providers_called.append("apify_zillow_empty")
    else:
        logger.info(
            "Apify Zillow scrape unavailable (outcome=%s) — continuing without photos",
            getattr(photos_result, "outcome", None)
            and photos_result.outcome.value or "unknown",
        )
        providers_called.append("apify_zillow_failed")

    if records:
        records[0]["photos"] = photos
        records[0]["photos_source"] = "apify_zillow" if photos else None

    # Verify
    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address", "living_sqft", "year_built"],
    )

    # P0-1 (Law #2): receipt for success exit path.
    # Risk tier is YELLOW because this response contains mortgage/ownership data
    # (owner name, mailing address, corporate indicator, absentee status).
    # TOOL_MATERIAL_PRICE_CHECK uses GREEN because it only touches product/price data.
    # Decision: YELLOW is intentional here, not an error. Auditor sign-off 2026-05-06.
    _emit_playbook_receipt(
        ctx=ctx,
        outcome_status="SUCCEEDED",
        reason_code="property_facts_success",
        playbook_name="PROPERTY_FACTS_AND_PERMITS",
        risk_tier="yellow",
        redacted_inputs={
            "address1": parsed.address1,
            "address2": parsed.address2,
        },
        redacted_outputs={
            "record_count": len(records),
            "artifact_type": "PropertyFactPack",
        },
    )
    return ResearchResponse(
        artifact_type="PropertyFactPack",
        summary=f"Property facts for {normalized_address}",
        records=records,
        sources=sources,
        freshness={"provider": "attom"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Add rental valuation", "Pull nearby sales comparables"],
        verification_report=report,
        segment="trades",
        intent="property_fact",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        providers_called=providers_called,
    )


async def execute_estimate_research(
    query: str, ctx: PlaybookContext, address: str = "",
) -> ResearchResponse:
    """ESTIMATE_RESEARCH — Support quoting with property facts + material pricing."""
    from aspire_orchestrator.providers.attom_client import execute_attom_property_detail
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    from aspire_orchestrator.services.adam.normalizers.property_normalizer import normalize_from_attom_detail
    from aspire_orchestrator.services.adam.normalizers.product_normalizer import normalize_from_serpapi_homedepot

    logger.info("Executing ESTIMATE_RESEARCH for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. ATTOM for property context
    if address:
        detail_result = await execute_attom_property_detail(
            payload={"address": address},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("attom")
        if detail_result.outcome.value == "success" and detail_result.data:
            prop = normalize_from_attom_detail(detail_result.data)
            records.append(prop.to_dict())
            sources.extend(prop.sources)

    # 2. SerpApi Home Depot for material pricing
    hd_result = await execute_serpapi_homedepot_search(
        payload={"query": query, "hd_sort": "price_low_to_high"},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("serpapi_home_depot")

    if hd_result.outcome.value == "success" and hd_result.data:
        for item in hd_result.data.get("results", [])[:8]:
            product = normalize_from_serpapi_homedepot(item)
            records.append(product.to_dict())
            sources.extend(product.sources)

    report = verify_records(records=records, sources=sources, required_fields=["normalized_address", "living_sqft"])

    return ResearchResponse(
        artifact_type="EstimateResearchPack",
        summary=f"Estimate research for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Compare with Google Shopping prices", "Find subcontractors for this job"],
        verification_report=report,
        segment="trades",
        intent="price_check",
        playbook="ESTIMATE_RESEARCH",
        providers_called=providers_called,
    )


def _fuzzy_pick_store_from_query(
    query: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick a store from a candidate list when the query mentions a street/area.

    Examples that resolve silently:
      "sheetrock at the Capital Circle one" -> store with "Capital Cir" in address
      "the Tenleytown Home Depot" -> store with "Tenleytown" in address

    Uses a simple substring match on normalized address tokens. If exactly one
    candidate's address shares a meaningful token (3+ chars, alpha) with the
    query, that's the pick. Multiple matches or none -> None (caller falls back).
    """
    import re as _re

    def _normalize_tokens(text: str) -> set[str]:
        return {t for t in _re.findall(r"[A-Za-z]{3,}", text.lower())}

    # Drop common stopwords that would create false positives.
    _STOPWORDS = {
        "home", "depot", "store", "the", "and", "near", "for", "from",
        "drive", "road", "street", "avenue", "boulevard", "lane", "way",
        "place", "court", "circle", "highway", "parkway", "terrace",
        "north", "south", "east", "west",
    }

    query_tokens = _normalize_tokens(query) - _STOPWORDS
    if not query_tokens:
        return None

    matches: list[dict[str, Any]] = []
    for store in candidates:
        addr_tokens = _normalize_tokens(store.get("address", "")) - _STOPWORDS
        if query_tokens & addr_tokens:
            matches.append(store)

    if len(matches) == 1:
        return dict(matches[0])
    return None


_GOOGLE_FORMATTED_ADDRESS_CITY_STATE_RE = re.compile(
    r",\s*([A-Za-z][A-Za-z\s\-\.']+?),\s*([A-Z]{2})\s+\d{5}",
)


# F-MED-6: ZIP regex used to autodetect a ZIP in the user's free-text query.
# Must NOT match product quantities like "10000 ft of pipe" or "20000 BTU".
# Anchors:
#   - Preceded by a comma + optional whitespace (matches "..., FL 32308")
#     OR by 2-letter state abbreviation + whitespace ("FL 32308" / "GA 30297")
#   - Followed by a non-digit boundary (so "30297-1234" matches "30297" then "-1234")
#   - Optional ZIP+4 tail
#   - 5-digit value within the legal US range (00501..99950)
_ZIP_IN_QUERY_RE = re.compile(
    r"(?:,\s*|\b[A-Z]{2}\s+)"
    r"(0050[1-9]|005[1-9]\d|00[6-9]\d{2}|0[1-9]\d{3}|[1-9]\d{4}(?<!00000))"
    r"(?:-\d{4})?"
    r"(?!\d)",
)


def _parse_city_state_from_formatted_address(addr: str) -> tuple[str, str]:
    """Extract (city, state) from a Google Places formattedAddress.

    Examples:
      "1490 Capital Circle Northwest, Tallahassee, FL 32303, USA"
        -> ("Tallahassee", "FL")
      "650 Stillwater Ave, Bangor, ME 04401, USA"
        -> ("Bangor", "ME")

    Returns ("", "") when the regex doesn't match — caller falls back to
    whatever city/state was already in store_summary.
    """
    if not addr:
        return "", ""
    match = _GOOGLE_FORMATTED_ADDRESS_CITY_STATE_RE.search(addr)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _build_store_disambiguation_response(
    query: str,
    candidates: list[dict[str, Any]],
    providers_called: list[str],
) -> ResearchResponse:
    """Return a StoreDisambiguation artifact so Ava + desktop can prompt for choice.

    F-HIGH-1: candidates ride at top-level `records` (consistent with all other
    artifacts: PriceComparison, PropertyFactPack, etc.) rather than nested
    inside `extra`. The desktop reads `records[]` uniformly, and the LLM
    response builder counts records to decide success/error states.
    """
    candidate_records: list[dict[str, Any]] = []
    for store in candidates:
        candidate_records.append({
            "card_kind": "store_candidate",
            "store_id": store.get("store_id", ""),
            "name": store.get("name", ""),
            "address": store.get("address", ""),
            "city": store.get("city", ""),
            "state": store.get("state", ""),
            "postal_code": store.get("postal_code", ""),
        })
    return ResearchResponse(
        artifact_type="StoreDisambiguation",
        summary=(
            f"Multiple Home Depot stores in {candidates[0].get('city', '')}. "
            "Which one would you like?"
        ),
        records=candidate_records,
        sources=[],
        freshness={"mode": "live"},
        confidence={"status": "verified", "score": 1.0},
        missing_fields=[],
        next_queries=[],
        segment="trades",
        intent="price_check",
        playbook="TOOL_MATERIAL_PRICE_CHECK",
        providers_called=providers_called,
        # `extra.candidates` retained for one release as a compatibility shim;
        # current callers (desktop AdamCardsRenderer) should switch to records.
        extra={"candidates": candidate_records, "query": query},
    )


def _redact_user_address(addr: str) -> str:
    """PII-safe representation of a user-provided address.

    F-HIGH-6: prior code logged `user_address[:60]` at INFO. Operators need a
    log signal that an address was supplied without exposing the address
    itself. We hash it (truncated) so identical inputs collapse into a single
    log line for correlation. ASPIRE_DEBUG_PII=1 reverts to a 60-char snippet
    for local debugging only.
    """
    import hashlib as _hashlib
    import os as _os

    if not addr:
        return ""
    if (_os.getenv("ASPIRE_DEBUG_PII") or "").strip().lower() in {"1", "true", "yes"}:
        return addr[:60]
    digest = _hashlib.sha256(addr.encode("utf-8")).hexdigest()[:10]
    return f"<addr:{len(addr)}c:{digest}>"


async def execute_tool_material_price_check(
    query: str,
    ctx: PlaybookContext,
    zip_code: str = "",
    store_id: str = "",
    on_sale: bool = False,
    voice_path: bool | None = None,
    city: str = "",
    state: str = "",
    user_address: str = "",
    include_other_stores: bool = False,
) -> ResearchResponse:
    """TOOL_MATERIAL_PRICE_CHECK - Find current pricing, stock, and store info for tools/materials.

    Strict policy for product cards:
      1. Run search with resolved location/store context.
      2. Retry with tightened query up to 3 attempts (text path) or single attempt (voice).
      3. Fail closed (no partial cards) if required product/store-summary fields are incomplete.

    Voice path budget: 5s end-to-end. When voice_path is True we run one attempt with
    a 4s SerpApi timeout and skip the Google Shopping cross-check entirely. When None,
    voice is auto-detected as "no zip + no store_id + no city" (Ava's typical voice query).

    Round 4 — user_address PRIMARY path (Task #43):
      - When `user_address` is provided (e.g. trades worker on a job site), we
        Geocode + Places searchNearby to pin the closest Home Depot to THAT
        address — not the office. Sets `delivery_zip` from the resolved store's
        postal_code and skips the city -> zip / multi-store disambiguation
        flow entirely. On any failure (timeout, no HD within 50km, API error)
        we fall through to the existing Wave A.5 path.

    Multi-store disambiguation (Wave A.5 / Task #32):
      - When `city` is set and the directory has multiple HD stores in (city, state):
        1. Try fuzzy-match the query against each store's address (e.g. "the one on
           Capital Circle" -> Capital Cir NE). If hit, silent auto-pick.
        2. Fall back to haversine via ctx.office_lat/office_lng. Pick closest within 50km.
        3. Otherwise return artifact_type="StoreDisambiguation" with candidate list.
      - When `store_id` is set explicitly: skip city -> zip; use that store directly.
    """
    import re as _re
    from aspire_orchestrator.providers.serpapi_shopping_client import execute_serpapi_shopping_search
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    from aspire_orchestrator.services.adam.normalizers.product_normalizer import (
        normalize_from_serpapi_shopping,
        normalize_from_serpapi_homedepot,
    )
    from aspire_orchestrator.services.adam.hd_store_directory import (
        lookup_store_by_id,
        lookup_zip_by_city,
        find_stores_in_city,
        find_nearest_store,
    )
    from aspire_orchestrator.services.adam.hd_store_resolver import resolve_store_async
    from aspire_orchestrator.services.adam.places_nearest_finder import (
        find_nearest_home_depot_by_address,
        NearestStore,
    )

    logger.info(
        "Executing TOOL_MATERIAL_PRICE_CHECK for: %s (city=%r state=%r store_id=%r zip=%r user_address=%s)",
        query[:80], city, state, store_id, zip_code, _redact_user_address(user_address or ""),
    )

    # F-CRIT-1: voice_path MUST be decided from caller-supplied signals BEFORE
    # the nearest-store resolver runs. The resolver pins zip_code from the
    # Google Places result, which would otherwise flip voice_path to False
    # mid-request (`not zip_code` → False) and route every Anam call into the
    # 3-attempt × 8s text loop (24s) inside the 5s voice budget.
    #
    # Inputs that count as "voice context": NO zip_code, NO store_id, NO city
    # at the public entry point. user_address by itself is a voice-friendly
    # signal (Anam's dynamic variable) so it does NOT flip voice_path off.
    if voice_path is None:
        # F-MED-6: tighter ZIP regex used here so a 5-digit product quantity
        # (e.g. "10000 ft of pipe") doesn't pre-populate zip_code and break
        # voice-path detection.
        query_zip_match = _ZIP_IN_QUERY_RE.search(query)
        query_has_zip = bool(query_zip_match)
        # Tightened from `\bin\s+([A-Za-z]+...)` which false-positived on
        # phrases like "in stock", "in store", "in house". A real city
        # reference must be followed by a 2-letter US state code (e.g.
        # "in Tallahassee, FL") or an explicit state name. Without that
        # discriminator, "show paint in stock at Home Depot" was flipping
        # voice_path to False, which re-enabled Google Shopping merging
        # — surfacing IMAGE-UNAVAILABLE Google Shopping cards alongside
        # real Home Depot inventory (May 4 user report).
        query_has_city = bool(
            _re.search(
                r"\bin\s+[A-Za-z][A-Za-z\s]+,\s*[A-Za-z]{2}\b",
                query,
            )
        )
        # user_address by itself is voice-friendly (Anam's dynamic variable)
        # and must NOT flip voice_path off. Even when user_address embeds a
        # zip we treat the request as voice path because the entry signal is
        # a single-line address from a voice session.
        if user_address and user_address.strip():
            voice_path = True
        else:
            voice_path = (
                not zip_code
                and not store_id
                and not city
                and not query_has_zip
                and not query_has_city
            )

    # Round 4 — PRIMARY path: nearest HD by user_address. When this resolves
    # successfully we pin delivery_zip from the resolved store's postal_code
    # and skip the city -> zip lookup entirely. On any failure we fall through
    # to Wave A.5 (city -> zip + multi-store disambiguation).
    #
    # The resolved NearestStore carries Google's formattedAddress + photo + a
    # haversine distance. Those override the static-directory fields in the
    # final store_summary because the user is at a job site — the Google
    # address is what they recognize, and distance_miles is hero data.
    nearest_store: NearestStore | None = None
    if user_address and user_address.strip():
        nearest_store = await find_nearest_home_depot_by_address(
            user_address.strip(),
            # Outer caller guard. Helper enforces an internal asyncio.wait_for
            # at the same value — keeping the boundary single-owned simplifies
            # cancellation semantics. Voice path budget is 5s end-to-end;
            # 3s here leaves 2s for SerpApi.
            timeout=3.0,
        )
        if nearest_store is not None:
            # Pin zip BEFORE the city/store_id branches below run.
            zip_code = nearest_store.postal_code or zip_code
            # Note: place_id is Google's, not a Home Depot store_id — we do
            # NOT set store_id from place_id (SerpApi rejects unknown ids).
            # The static directory still gets a chance to resolve store_id
            # from pickup.store_id in the SerpApi response below.

    if not zip_code:
        zip_match = _ZIP_IN_QUERY_RE.search(query)
        if zip_match:
            zip_code = zip_match.group(1)

    location_hint = ""
    if city:
        location_hint = f"{city}, {state}".strip(", ") if state else city
    else:
        city_match = _re.search(r"\bin\s+([A-Za-z][A-Za-z\s]+(?:,\s*[A-Za-z]{2})?)\b", query)
        if city_match:
            location_hint = city_match.group(1).strip(" .,")

    # Wave A.5: explicit store_id beats city/zip — use the directory record directly.
    if store_id:
        directory_record = lookup_store_by_id(store_id)
        if directory_record:
            zip_code = zip_code or directory_record.get("postal_code", "")
    elif city:
        # Wave A.5: multi-store disambiguation in a city.
        candidates = find_stores_in_city(city, state or None)
        if len(candidates) > 1:
            # (a) fuzzy address-hint auto-pick (e.g. "the one on Capital Circle").
            picked = _fuzzy_pick_store_from_query(query, candidates)
            # (b) haversine via office_lat/office_lng if available.
            if picked is None:
                office_lat = getattr(ctx, "office_lat", None)
                office_lng = getattr(ctx, "office_lng", None)
                if office_lat is not None and office_lng is not None:
                    nearest = find_nearest_store(
                        float(office_lat), float(office_lng),
                        city=city, state=state or None, max_km=50.0,
                    )
                    if nearest:
                        picked = nearest
            # (c) no hint and no office address -> return disambiguation artifact.
            if picked is None:
                _emit_playbook_receipt(
                    ctx=ctx,
                    outcome_status="SUCCEEDED",
                    reason_code="store_disambiguation",
                    playbook_name="TOOL_MATERIAL_PRICE_CHECK",
                    summary={
                        "candidate_count": len(candidates),
                        "city": city,
                        "state": state,
                    },
                )
                return _build_store_disambiguation_response(
                    query=query, candidates=candidates, providers_called=[],
                )
            zip_code = zip_code or picked.get("postal_code", "")
            store_id = picked.get("store_id", "")
        elif len(candidates) == 1 and not zip_code:
            zip_code = candidates[0].get("postal_code", "") or zip_code
            store_id = store_id or candidates[0].get("store_id", "")
        elif not zip_code:
            # City→zip lookup (Wave A.2). Single primary path. No fallback chain.
            looked_up = lookup_zip_by_city(city, state or None)
            if looked_up:
                zip_code = looked_up

    def _product_missing_fields(r: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for field in ("product_name", "price", "url", "image_url", "retailer"):
            v = r.get(field)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing.append(field)
        return missing

    def _store_missing_fields(store: dict[str, Any]) -> list[str]:
        # Irreducible contract: store_name. Without a name the card has no
        # identity worth showing. Everything else (address, phone, website) is
        # supplementary metadata — present when populated, omitted gracefully
        # when not.
        #
        # Phone + website were dropped first because Google Places
        # /details/json enrichment was unreliable. Address followed because
        # the same resolver populates it AND the resolver was returning
        # empty fields for no-zip city queries (common Anam path: "find
        # paint sprayers in Tallahassee"). The card UI handles missing
        # fields cleanly — users get the products they asked for and the
        # store's identifiable name (correct via Pass 1.1 — pickup.store_name
        # / search_information.store_name).
        #
        # Follow-up tracked separately: fix the Google Places resolver path
        # for no-zip queries so address/phone/website reliably populate.
        missing: list[str] = []
        for field in ("store_name",):
            v = store.get(field)
            if v is None or (isinstance(v, str) and not str(v).strip()):
                missing.append(field)
        return missing

    providers_called: list[str] = []
    last_missing_fields: list[str] = []
    final_records: list[dict[str, Any]] = []
    final_sources: list[SourceAttribution] = []
    final_store_summary: dict[str, Any] = {}
    # Search-level metadata captured from SerpAPI for refinable carousels —
    # surface taxonomy breadcrumbs, filter tokens, and related queries.
    final_taxonomy: list[dict[str, Any]] = []
    final_filters: list[dict[str, Any]] = []
    final_related_products: list[Any] = []
    final_pagination: dict[str, Any] = {}

    # Track Round-4 provider call for cost attribution. The helper itself does
    # not emit a receipt — the playbook wrapper at server.py records the call
    # via providers_called, and Outcome rolls up to SUCCESS/FAILED on the
    # whole playbook. Logging both branches preserves Law #2 evidence.
    if user_address and user_address.strip():
        if nearest_store is not None:
            providers_called.append("google_places_nearest")
            logger.info(
                "Round-4 nearest HD resolved: %s (zip=%s, %.1fmi from user)",
                nearest_store.name, nearest_store.postal_code,
                nearest_store.distance_miles,
            )
        else:
            providers_called.append("google_places_nearest_failed")
            logger.info(
                "Round-4 nearest HD lookup returned None for user_address=%r — "
                "falling through to Wave A.5 (city -> zip)",
                _redact_user_address(user_address),
            )

    # voice_path was already finalized at the top of this function from the
    # caller-supplied signals. Don't re-derive it here — that would let the
    # nearest_store-pinned zip_code flip it back to False (the F-CRIT-1 bug).
    # Round 7 A.2 — multi-store gate. Voice path is HD-only by default; user
    # explicit opt-in (include_other_stores=True) re-enables Google Shopping
    # even on the voice path. Non-voice path always runs shopping.
    run_shopping = (not voice_path) or include_other_stores
    if voice_path:
        query_attempts = [query]
        # When include_other_stores=True we add ~1.5s budget for the shopping
        # retry-with-backoff path; HD timeout stays at 4s so total stays under
        # the 4.5s P95 voice SLO.
        hd_timeout = 4.0
        skip_google_shopping = not run_shopping
    else:
        query_attempts = [
            query,
            f"{query} Home Depot",
            f"{query} Home Depot {location_hint}".strip(),
        ]
        hd_timeout = 8.0
        skip_google_shopping = not run_shopping

    for attempt_idx, attempt_query in enumerate(query_attempts, start=1):
        records: list[dict[str, Any]] = []
        sources: list[SourceAttribution] = []
        hd_store_info: dict[str, Any] = {}
        resolved_store_id = store_id

        shopping_payload: dict[str, Any] = {"query": attempt_query, "sort_by": 1}
        if zip_code:
            shopping_payload["location"] = zip_code
        elif location_hint:
            shopping_payload["location"] = location_hint
        if on_sale:
            shopping_payload["on_sale"] = True

        async def _resolve_and_search_hd() -> Any:
            nonlocal resolved_store_id, hd_store_info
            if not resolved_store_id and (zip_code or location_hint):
                # Voice path budget is 4s end-to-end. Cap the resolver at 1.5s so
                # a slow Google Places call cannot consume the entire window —
                # the static directory + SerpApi response carry the card even
                # when phone/website/image_url enrichment times out.
                resolver_coro = resolve_store_async(
                    zip_code=zip_code,
                    location_hint=location_hint,
                    correlation_id=ctx.correlation_id,
                    suite_id=ctx.suite_id,
                    office_id=ctx.office_id,
                )
                store_match: dict[str, Any] | None
                if voice_path:
                    try:
                        store_match = await asyncio.wait_for(resolver_coro, timeout=1.5)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Voice path: resolve_store_async timed out at 1.5s — "
                            "continuing without enrichment"
                        )
                        store_match = None
                else:
                    store_match = await resolver_coro
                if store_match:
                    resolved_store_id = str(store_match.get("store_id", "")).strip()
                    hd_store_info = dict(store_match)

            # Hard guardrail: refuse to call SerpAPI blind. Without store_id
            # OR delivery_zip, SerpAPI silently injects its account-default
            # (store_id=2414, zip=04401, Bangor ME) and ships poisoned results.
            # We return a synthetic FAILED outcome so the loop falls through to
            # the STORE_UNRESOLVED decision flag — Ava asks the user to clarify.
            if not resolved_store_id and not zip_code:
                logger.warning(
                    "TOOL_MATERIAL_PRICE_CHECK: refusing blind SerpAPI call — "
                    "no store_id and no delivery_zip resolved (query=%r). "
                    "Returning STORE_UNRESOLVED.",
                    attempt_query[:80],
                )
                providers_called.append("store_unresolved")
                return None

            hd_payload: dict[str, Any] = {"query": attempt_query, "hd_sort": "best_match"}
            if resolved_store_id:
                hd_payload["store_id"] = resolved_store_id
            if zip_code:
                hd_payload["delivery_zip"] = zip_code
            return await execute_serpapi_homedepot_search(
                payload=hd_payload,
                correlation_id=ctx.correlation_id,
                suite_id=ctx.suite_id,
                office_id=ctx.office_id,
                timeout=hd_timeout,
            )

        # Round 7 A.2 — SerpApi shopping with exponential backoff + jitter on 429.
        # Max 2 retries (3 total attempts), then graceful degrade to None so the
        # HD result still carries the response. Receipt for the rate-limited
        # outcome is emitted by the SerpApi client per call AND captured in the
        # playbook rollup receipt below.
        async def _shopping_with_backoff() -> Any:
            attempts = _SHOPPING_RETRY_MAX_ATTEMPTS + 1
            last_result: Any = None
            for attempt in range(1, attempts + 1):
                try:
                    res = await execute_serpapi_shopping_search(
                        payload=shopping_payload,
                        correlation_id=ctx.correlation_id,
                        suite_id=ctx.suite_id,
                        office_id=ctx.office_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_result = exc
                    break
                last_result = res
                # Detect 429 / RATE_LIMITED in the structured error string.
                err_str = ""
                if hasattr(res, "error") and isinstance(res.error, str):
                    err_str = res.error.upper()
                rate_limited = (
                    res.outcome.value != "success"
                    and ("RATE_LIMITED" in err_str or "429" in err_str)
                )
                if not rate_limited or attempt >= attempts:
                    return res
                # Exponential backoff with jitter — 250ms, 500ms (+ 0-100ms).
                base_ms = (
                    _SHOPPING_RETRY_BASE_MS[min(attempt - 1, len(_SHOPPING_RETRY_BASE_MS) - 1)]
                )
                jitter_ms = _random.randint(0, 100)
                await asyncio.sleep((base_ms + jitter_ms) / 1000.0)
            return last_result

        if skip_google_shopping:
            hd_result = await _resolve_and_search_hd()
            shopping_result = None
        else:
            hd_result, shopping_result = await asyncio.gather(
                _resolve_and_search_hd(),
                _shopping_with_backoff(),
                return_exceptions=True,
            )

        if "serpapi_home_depot" not in providers_called:
            providers_called.append("serpapi_home_depot")
        if not skip_google_shopping and "serpapi_shopping" not in providers_called:
            providers_called.append("serpapi_shopping")

        # F-MED-4: distinguish hard cancellation/timeout (break loop, retry
        # would only make latency worse) from soft failures (continue to next
        # tightened attempt). asyncio.TimeoutError, asyncio.CancelledError =>
        # break. Generic Exception => log and continue.
        if isinstance(hd_result, (asyncio.TimeoutError, asyncio.CancelledError)):
            logger.warning(
                "TOOL_MATERIAL_PRICE_CHECK attempt=%s aborted by %s — breaking retry loop",
                attempt_idx, type(hd_result).__name__,
            )
            break
        if isinstance(hd_result, Exception):
            logger.warning(
                "TOOL_MATERIAL_PRICE_CHECK attempt=%s soft error: %s",
                attempt_idx, hd_result,
            )

        # Bangor guardrail short-circuit (Wave 2.0). The resolver returned
        # None when neither store_id nor delivery_zip could be resolved — we
        # must NOT call SerpAPI blind. Break out so the response surfaces
        # `reason_code=store_unresolved` and Ava asks the user to clarify
        # location instead of shipping Bangor-poisoned results.
        if hd_result is None:
            break

        # F-HIGH-7: SerpApi 429 — retrying just burns budget AND quota. Break
        # immediately and surface a structured rate-limit response. Reason
        # comes back from the underlying SerpApi client (response.error_code).
        if (
            not isinstance(hd_result, Exception)
            and hd_result.outcome.value != "success"
            and (
                hasattr(hd_result, "error")
                and isinstance(hd_result.error, str)
                and "RATE_LIMITED" in hd_result.error.upper()
            )
        ):
            logger.warning(
                "TOOL_MATERIAL_PRICE_CHECK: SerpApi RATE_LIMITED on attempt=%s — short-circuiting retries",
                attempt_idx,
            )
            providers_called.append("serpapi_home_depot_rate_limited")
            break

        if not isinstance(hd_result, Exception) and hd_result.outcome.value == "success" and hd_result.data:
            serpapi_store = hd_result.data.get("store", {})

            # Bangor guardrail (Wave 2.0).
            # SerpAPI silently injects its account-default store_id=2414 +
            # delivery_zip=04401 (Bangor, ME) when the caller supplies neither.
            # Every product in the response then reads pickup.store_name="Bangor"
            # (or "South Loop" depending on SerpAPI's whim) regardless of where
            # the user actually is. We REFUSE these results — Ava asks the user
            # to clarify location instead of shipping cards anchored to Maine.
            #
            # Two flavors:
            #   1. default_store_fallback (no zip + no store_id passed at all)
            #      = TRUE poisoning, no usable store identity → refuse entirely.
            #   2. pickup_poisoning (zip was passed but products still ship
            #      Bangor pickup data) = the products are valid for delivery,
            #      but in-store inventory data is wrong → strip pickup, keep
            #      products with "free ship to store" path.
            true_poisoning = bool(serpapi_store.get("default_store_fallback")) and not bool(serpapi_store.get("pickup_poisoning"))
            pickup_only_poisoning = bool(serpapi_store.get("pickup_poisoning"))

            if true_poisoning:
                logger.warning(
                    "TOOL_MATERIAL_PRICE_CHECK: SerpAPI default-fallback (Bangor) "
                    "detected on attempt=%s — refusing results, query=%r will be "
                    "retried with a STORE_UNRESOLVED decision flag",
                    attempt_idx, attempt_query[:80],
                )
                providers_called.append("serpapi_home_depot_default_fallback")
                # Treat as if the call failed — let the loop break out to the
                # store_unresolved decision flag below.
                continue

            if pickup_only_poisoning:
                logger.warning(
                    "TOOL_MATERIAL_PRICE_CHECK: pickup-data poisoning on attempt=%s "
                    "(zip=%s honored but pickup ships Bangor) — stripping per-product "
                    "fulfillment_pickup so cards show 'free ship to store' instead of "
                    "lying about Bangor inventory",
                    attempt_idx, zip_code,
                )
                providers_called.append("serpapi_home_depot_pickup_poisoned")
                # Strip pickup data from each raw product BEFORE normalization.
                # Products keep title/price/rating/delivery — just no false
                # "11 in stock at Bangor" claim.
                for raw_p in hd_result.data.get("results", []):
                    pickup_obj = raw_p.get("pickup") if isinstance(raw_p.get("pickup"), dict) else {}
                    if pickup_obj:
                        # Preserve free_ship_to_store flag (location-agnostic).
                        keep = {}
                        if pickup_obj.get("free_ship_to_store"):
                            keep["free_ship_to_store"] = True
                        raw_p["pickup"] = keep

            if serpapi_store.get("store_name"):
                hd_store_info["store_name"] = serpapi_store["store_name"]
            if not hd_store_info.get("store_id") and serpapi_store.get("store_id"):
                hd_store_info["store_id"] = serpapi_store["store_id"]

            # Primary store-identity path: read pickup.store_id from the first
            # product and resolve name + address from the static directory.
            # This is deterministic and doesn't depend on Google Places. Phone
            # and website remain optional enrichment (Task #20).
            raw_results = hd_result.data.get("results", [])
            if raw_results:
                pickup = raw_results[0].get("pickup") or {}
                pickup_store_id = (
                    str(pickup.get("store_id", "")).strip()
                    or str(serpapi_store.get("store_id", "")).strip()
                )
                if pickup_store_id:
                    directory_record = lookup_store_by_id(pickup_store_id)
                    if directory_record:
                        # Static directory wins for name + address fields.
                        hd_store_info["store_id"] = directory_record["store_id"]
                        hd_store_info["store_name"] = directory_record["name"]
                        hd_store_info["address"] = directory_record["address"]
                        hd_store_info["city"] = directory_record["city"]
                        hd_store_info["state"] = directory_record["state"]
                        hd_store_info["postal_code"] = directory_record["postal_code"]
                        resolved_store_id = directory_record["store_id"]
                    else:
                        logger.warning(
                            "HD store_id %s not in static directory — "
                            "falling back to SerpApi store name",
                            pickup_store_id,
                        )

            for item in raw_results[:8]:
                product = normalize_from_serpapi_homedepot(item)
                records.append(product.to_dict())
                sources.extend(product.sources)

        if (
            shopping_result is not None
            and not isinstance(shopping_result, Exception)
            and shopping_result.outcome.value == "success"
            and shopping_result.data
        ):
            for item in shopping_result.data.get("results", [])[:6]:
                product = normalize_from_serpapi_shopping(item)
                records.append(product.to_dict())
                sources.extend(product.sources)

        # Round 7 A.2 — when include_other_stores=True, do NOT filter to HD-only;
        # show all retailers (Lowe's, Walmart, Ace, Amazon) in carousel. HD
        # products still drive the success-completion check below so the store
        # summary card stays anchored on a real HD store.
        if include_other_stores:
            display_products = list(records)
        else:
            display_products = [r for r in records if r.get("retailer") == "Home Depot"]
        hd_products = [r for r in records if r.get("retailer") == "Home Depot"]
        complete_products = [r for r in display_products if not _product_missing_fields(r)]
        # Sub-item 1.1: surface SerpApi search_information.store_name as
        # store_summary.name so the store-summary card has the correct local
        # store label even when the resolver disagrees with SerpApi's pin.
        store_summary = {
            "card_kind": "store_summary",
            "store_id": hd_store_info.get("store_id", ""),
            "store_name": hd_store_info.get("store_name", ""),
            "name": hd_store_info.get("store_name", ""),
            "address": hd_store_info.get("address", ""),
            "city": hd_store_info.get("city", ""),
            "state": hd_store_info.get("state", ""),
            "postal_code": hd_store_info.get("postal_code", ""),
            "phone": hd_store_info.get("phone", ""),
            "website": hd_store_info.get("website", ""),
            "image_url": hd_store_info.get("image_url", ""),
            "open_now": hd_store_info.get("open_now"),
            "rating": hd_store_info.get("rating"),
            "retailer": "Home Depot",
        }

        # Round 4: when nearest_store is set (Google Places searchText found
        # the actual nearest HD to the user's job site), it's AUTHORITATIVE
        # for the user-visible card. Overwrite name/address/city/state/zip
        # with the Google result. The previous logic kept hd_store_info's
        # values (which come from SerpApi search_information.store_name and
        # may be a stale account-default like "Bangor"), causing the card
        # address to show Tallahassee but the store name to say "Bangor".
        if nearest_store is not None:
            store_summary["name"] = nearest_store.name or store_summary["name"]
            store_summary["store_name"] = nearest_store.name or store_summary["store_name"]
            store_summary["address"] = nearest_store.address or store_summary["address"]
            store_summary["postal_code"] = nearest_store.postal_code or store_summary["postal_code"]
            # Parse city + state from Google's formattedAddress (e.g.
            # "1490 Capital Cir NW, Tallahassee, FL 32303, USA"). Only
            # overwrite when the parse succeeds; otherwise fall back.
            parsed_city, parsed_state = _parse_city_state_from_formatted_address(
                nearest_store.address,
            )
            if parsed_city:
                store_summary["city"] = parsed_city
            if parsed_state:
                store_summary["state"] = parsed_state
            if nearest_store.photo_url:
                store_summary["image_url"] = nearest_store.photo_url
            # F-MED-7: distance_miles is None on the searchText primary path
            # (no user coords). Only emit the field when we actually computed
            # it via haversine — the card UI shows "—" when missing.
            if nearest_store.distance_miles is not None and nearest_store.distance_miles > 0:
                store_summary["distance_miles"] = round(nearest_store.distance_miles, 1)
            # store_id: keep static directory's SerpApi store_id when present
            # (used by downstream enrichment); Google place_id only as fallback
            # for cases where the static directory had no match.
            if not store_summary.get("store_id"):
                store_summary["store_id"] = nearest_store.place_id

        store_missing = _store_missing_fields(store_summary)
        last_missing_fields = sorted({
            *[m for r in hd_products for m in _product_missing_fields(r)],
            *[f"store_summary.{f}" for f in store_missing],
        })

        logger.info(
            "TOOL_MATERIAL_PRICE_CHECK attempt=%s hd_products=%s complete_hd_products=%s store_missing=%s",
            attempt_idx, len(hd_products), len(complete_products), store_missing,
        )

        if complete_products and not store_missing:
            # Propagate the resolved closest-store identity into each product
            # so the UI shows "In stock at Capital Circle Northeast" instead
            # of "In stock at Tallahassee" (the SerpApi pickup.store_name
            # default was the city, not the actual store name — May 4 user
            # report). Only override when we have a real resolved name.
            resolved_store_name = (
                store_summary.get("name")
                or store_summary.get("store_name")
                or ""
            )
            resolved_store_id = store_summary.get("store_id") or ""
            if resolved_store_name:
                for product in complete_products:
                    if product.get("retailer") == "Home Depot":
                        product["store_name"] = resolved_store_name
                        if resolved_store_id and not product.get("store_id"):
                            product["store_id"] = resolved_store_id

            # Card pack is Home Depot-anchored. When include_other_stores=True,
            # also include non-HD complete products from display_products so the
            # carousel mixes retailers; when False, HD-only.
            final_records = [store_summary, *complete_products]
            final_sources = sources
            final_store_summary = store_summary
            # Capture search-level metadata for refinable session UI. Cap
            # filters at top-12 facets to keep payload bounded; the prompt
            # reads this for "show only Milwaukee under $200" follow-ups.
            if not isinstance(hd_result, Exception) and hd_result.data:
                hd_data = hd_result.data
                final_taxonomy = list(hd_data.get("taxonomy") or [])[:6]
                final_filters = list(hd_data.get("filters") or [])[:12]
                final_related_products = list(hd_data.get("related_products") or [])[:8]
                final_pagination = hd_data.get("pagination") or {}
            break

    # Round 7 A.2 — decision flags computed for EVERY response (error or success).
    # Prompt's FETCH MODE rule (Wave C.4) reads these to decide whether to offer
    # Lowe's/Ace fallback. None means "unknown" (no HD resolved yet).
    nearest_distance: float | None = (
        round(nearest_store.distance_miles, 1)
        if (nearest_store is not None and nearest_store.distance_miles is not None)
        else None
    )
    # hd_too_far: True when (a) we have a distance and it exceeds threshold,
    # OR (b) we tried to resolve nearest HD via user_address and got nothing
    # (which means no HD within 50km — also "too far"). Default False when no
    # user_address was supplied (we have no signal).
    if nearest_store is None and user_address and user_address.strip():
        hd_too_far = True
    elif nearest_distance is not None and nearest_distance > HD_TOO_FAR_MILES:
        hd_too_far = True
    else:
        hd_too_far = False

    # hd_has_stock: True when at least one HD product (NOT non-HD) has in_store_stock > 0.
    hd_in_stock_count = sum(
        1 for r in final_records
        if r.get("retailer") == "Home Depot"
        and r.get("card_kind") != "store_summary"
        and isinstance(r.get("in_store_stock"), (int, float))
        and r["in_store_stock"] > 0
    )
    hd_has_stock = hd_in_stock_count > 0

    decision_flags: dict[str, Any] = {
        "nearest_store_distance_miles": nearest_distance,
        "hd_too_far": hd_too_far,
        "hd_has_stock": hd_has_stock,
        "include_other_stores": include_other_stores,
    }

    if not final_records:
        # Error / no-match path. Receipt + decision flags still emitted so the
        # prompt can decide the next step. Reason code distinguishes shopping-429
        # (when SerpApi short-circuited) from generic no-match.
        if "serpapi_home_depot_rate_limited" in providers_called:
            reason_code = "shopping_429"
        elif (
            "store_unresolved" in providers_called
            or "serpapi_home_depot_default_fallback" in providers_called
        ):
            # Bangor guardrail fired — no usable store identity, refused to
            # ship poisoned results. Surface this so the prompt asks the user
            # for their zip / city / job-site address.
            reason_code = "store_unresolved"
            decision_flags["store_unresolved"] = True
        elif hd_too_far:
            reason_code = "hd_too_far"
        elif not hd_has_stock and hd_products:
            reason_code = "no_stock"
        else:
            reason_code = "missing_required_fields"

        _emit_playbook_receipt(
            ctx=ctx,
            outcome_status="FAILED",
            reason_code=reason_code,
            playbook_name="TOOL_MATERIAL_PRICE_CHECK",
            summary={
                **decision_flags,
                "providers_called": providers_called,
                "missing_fields": last_missing_fields,
            },
        )

        return ResearchResponse(
            artifact_type="error",
            summary="I could not retrieve complete Home Depot product and store details right now. Please try again in a moment.",
            records=[],
            sources=[],
            freshness={"mode": "live"},
            confidence={"status": "unverified", "score": 0.0},
            missing_fields=last_missing_fields,
            next_queries=["Try again in a moment", "Use a different city or ZIP"],
            segment="trades",
            intent="price_check",
            playbook="TOOL_MATERIAL_PRICE_CHECK",
            providers_called=providers_called,
            extra={
                "hard_fail": True,
                "missing_fields": last_missing_fields,
                **decision_flags,
            },
        )

    report = verify_records(records=final_records, sources=final_sources, required_fields=["product_name", "price", "retailer"])

    hd_count = sum(1 for r in final_records if r.get("retailer") == "Home Depot" and r.get("card_kind") != "store_summary")
    other_count = sum(
        1 for r in final_records
        if r.get("retailer") and r.get("retailer") != "Home Depot"
        and r.get("card_kind") != "store_summary"
    )
    in_stock = hd_in_stock_count
    summary_parts = [f"Price check for {query[:60]}"]
    if final_store_summary.get("store_name"):
        summary_parts.append(
            f"Home Depot store: {final_store_summary['store_name']} (#{final_store_summary.get('store_id', '')})"
        )
    summary_parts.append(f"{hd_count} Home Depot products, {in_stock} in stock")
    if include_other_stores and other_count:
        summary_parts.append(f"{other_count} other-retailer products via Google Shopping")

    success_reason = "multi_store_success" if include_other_stores else "success"
    _emit_playbook_receipt(
        ctx=ctx,
        outcome_status="SUCCEEDED",
        reason_code=success_reason,
        playbook_name="TOOL_MATERIAL_PRICE_CHECK",
        summary={
            **decision_flags,
            "providers_called": providers_called,
            "hd_product_count": hd_count,
            "other_product_count": other_count,
        },
    )

    return ResearchResponse(
        artifact_type="PriceComparison",
        summary=". ".join(summary_parts) + ".",
        records=final_records,
        sources=final_sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=[
            f"Compare prices at Lowe's near {zip_code}" if zip_code else "Compare at other retailers",
            "Check for current sales and promotions",
        ],
        verification_report=report,
        segment="trades",
        intent="price_check",
        playbook="TOOL_MATERIAL_PRICE_CHECK",
        providers_called=providers_called,
        extra={
            "store_summary": final_store_summary,
            "cards_version": "v1",
            # Search-level metadata for refinable carousel sessions.
            # taxonomy = breadcrumbs ("Tools > Power Tools > Drills")
            # filters  = facets w/ hd_filter_tokens for "narrow to Milwaukee"
            # related_products = query suggestions ("ryobi cordless drill")
            "taxonomy": final_taxonomy,
            "filters": final_filters,
            "related_products": final_related_products,
            "pagination": final_pagination,
            **decision_flags,
        },
    )

async def execute_competitor_pricing_scan(
    query: str, ctx: PlaybookContext, location: str = "",
) -> ResearchResponse:
    """COMPETITOR_PRICING_SCAN — Map local competitors and pricing signals."""
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import normalize_from_google_places
    from aspire_orchestrator.services.adam.normalizers.web_normalizer import normalize_from_exa

    logger.info("Executing COMPETITOR_PRICING_SCAN for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. Google Places for local competitors
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 2. Exa deep-lite for competitor intelligence with structured output
    exa_result = await execute_exa_search(
        payload={
            "query": f"competitor pricing analysis {query}",
            "type": "deep-lite",
            "category": "company",
            "num_results": 5,
            "moderation": True,
        },
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("exa")

    exa_grounding: list[dict[str, Any]] = []
    if exa_result.outcome.value == "success" and exa_result.data:
        for r in exa_result.data.get("results", [])[:5]:
            we = normalize_from_exa(r)
            records.append(we.to_dict())
            sources.append(SourceAttribution(provider="exa"))
        exa_grounding = exa_result.data.get("grounding", [])

    report = verify_records(
        records=records, sources=sources,
        required_fields=["name", "normalized_address"],
        exa_grounding=exa_grounding,
    )

    return ResearchResponse(
        artifact_type="CompetitorBrief",
        summary=f"Competitor scan for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Deep dive on top competitor", "Compare pricing models"],
        verification_report=report,
        segment="trades",
        intent="compare",
        playbook="COMPETITOR_PRICING_SCAN",
        providers_called=providers_called,
    )


async def execute_subcontractor_scout(
    query: str, ctx: PlaybookContext, location: str = "",
) -> ResearchResponse:
    """SUBCONTRACTOR_SCOUT — Find nearby subcontractors by trade."""
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import (
        normalize_from_google_places,
        normalize_from_foursquare,
    )

    logger.info("Executing SUBCONTRACTOR_SCOUT for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. Google Places
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 2. Foursquare for additional coverage
    fs_result = await execute_foursquare_search(
        payload={"query": query, "near": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("foursquare")

    if fs_result.outcome.value == "success" and fs_result.data:
        for place in fs_result.data.get("results", [])[:5]:
            biz = normalize_from_foursquare(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    report = verify_records(
        records=records, sources=sources,
        required_fields=["name", "normalized_address", "phone"],
    )

    return ResearchResponse(
        artifact_type="VendorShortlist",
        summary=f"Subcontractor search for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Verify licensing", "Check reviews in detail"],
        verification_report=report,
        segment="trades",
        intent="lookup",
        playbook="SUBCONTRACTOR_SCOUT",
        providers_called=providers_called,
    )


async def execute_territory_opportunity_scan(
    query: str, ctx: PlaybookContext, geo_scope: str = "",
) -> ResearchResponse:
    """TERRITORY_OPPORTUNITY_SCAN — Identify promising ZIPs by density + activity."""
    from aspire_orchestrator.providers.attom_client import execute_attom_sales_trends
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import normalize_from_google_places
    from aspire_orchestrator.services.adam.normalizers.web_normalizer import normalize_from_exa

    logger.info("Executing TERRITORY_OPPORTUNITY_SCAN for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. ATTOM sales trends for market activity
    if geo_scope:
        trends_result = await execute_attom_sales_trends(
            payload={"geoid": geo_scope, "geo_type": "ZI"},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("attom")
        if trends_result.outcome.value == "success" and trends_result.data:
            records.append({"type": "market_trends", "data": trends_result.data})
            sources.append(SourceAttribution(provider="attom"))

    # 2. Google Places for competitor density
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": geo_scope},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 3. Exa for market intelligence
    exa_result = await execute_exa_search(
        payload={
            "query": f"market opportunity {query} {geo_scope}",
            "type": "deep-lite",
            "num_results": 5,
        },
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("exa")

    if exa_result.outcome.value == "success" and exa_result.data:
        for r in exa_result.data.get("results", [])[:5]:
            we = normalize_from_exa(r)
            records.append(we.to_dict())
            sources.append(SourceAttribution(provider="exa"))

    report = verify_records(records=records, sources=sources)

    return ResearchResponse(
        artifact_type="TerritoryAnalysis",
        summary=f"Territory scan for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Drill into top ZIP code", "Compare adjacent territories"],
        verification_report=report,
        segment="trades",
        intent="territory_scan",
        playbook="TERRITORY_OPPORTUNITY_SCAN",
        providers_called=providers_called,
    )

