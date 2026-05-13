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

# ZIP code regex used for voice-path detection and address parsing.
# Tightened from \d{5} to require word boundaries to avoid false-positives
# from 5-digit quantities in product queries.
_ZIP_IN_QUERY_RE = re.compile(r"\b(\d{5})\b(?!-\d{4})")


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
    """Emit an immutable playbook receipt (Law #2). Fire-and-forget."""
    import uuid as _uuid2
    from aspire_orchestrator.services.receipt_store import store_receipts
    receipt_id = str(_uuid2.uuid4())
    receipt: dict[str, Any] = {
        "id": receipt_id,
        "receipt_type": f"{playbook_name}.playbook",
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "tenant_id": ctx.tenant_id,
        "outcome": outcome_status.lower(),
        "action_type": f"{playbook_name}.execute",
        "tool_used": playbook_name,
        "risk_tier": risk_tier,
        "reason_code": reason_code,
        "trace_id": ctx.correlation_id,
        "correlation_id": ctx.correlation_id,
        "capability_token_id": ctx.capability_token_id or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if summary:
        receipt["redacted_outputs"] = summary
    if redacted_inputs:
        receipt["redacted_inputs"] = redacted_inputs
    if redacted_outputs:
        receipt.setdefault("redacted_outputs", {}).update(redacted_outputs)
    try:
        store_receipts([receipt])
    except Exception:
        pass  # Receipt write failure must never block the playbook


def _redact_user_address(addr: str) -> str:
    """Redact street number from user_address for log safety (Law #9)."""
    if not addr:
        return ""
    # Keep city/state/zip, redact house number.
    parts = addr.split(",", 1)
    if len(parts) > 1:
        return f"<ADDR_REDACTED>,{parts[1]}"
    return "<ADDR_REDACTED>"


def _parse_city_state_from_formatted_address(formatted: str) -> tuple[str, str]:
    """Parse city and state from a Google formattedAddress string.

    Example: "1490 Capital Cir NW, Tallahassee, FL 32303, USA"
    Returns ("Tallahassee", "FL") or ("", "") on parse failure.
    """
    if not formatted:
        return ("", "")
    # Split by comma, look for "City, ST ZIP" pattern
    parts = [p.strip() for p in formatted.split(",")]
    for i, part in enumerate(parts):
        # Part looks like "FL 32303" or "FL" — state is before ZIP
        state_zip = re.match(r"^([A-Z]{2})\s*\d{0,5}$", part)
        if state_zip and i > 0:
            state = state_zip.group(1)
            city = parts[i - 1]
            return (city, state)
    return ("", "")


# ---------------------------------------------------------------------------
# PROPERTY_FACTS_AND_PERMITS playbook
# ---------------------------------------------------------------------------


async def execute_property_facts_and_permits(
    query: str,
    ctx: PlaybookContext,
    address: str = "",
) -> ResearchResponse:
    """PROPERTY_FACTS_AND_PERMITS — ATTOM + Apify Zillow for property facts."""
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_property_detail,
        execute_attom_permit_history,
    )
    from aspire_orchestrator.providers.apify_zillow_client import (
        execute_apify_zillow_photos,
    )
    from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
        normalize_from_attom_detail,
        normalize_from_attom_permits,
    )
    from aspire_orchestrator.services.adam.normalizers.zillow_photo_normalizer import (
        normalize_zillow_photos,
    )

    logger.info("Executing PROPERTY_FACTS_AND_PERMITS for: %s address=%r", query[:80], address[:60] if address else "")

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. ATTOM property detail
    if address:
        detail_result = await execute_attom_property_detail(
            payload={"address": address},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("attom_detail")
        if detail_result.outcome.value == "success" and detail_result.data:
            prop = normalize_from_attom_detail(detail_result.data)
            records.append(prop.to_dict())
            sources.extend(prop.sources)

    # 2. ATTOM permits
    if address:
        permit_result = await execute_attom_permit_history(
            payload={"address": address},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("attom_permits")
        if permit_result.outcome.value == "success" and permit_result.data:
            permits = normalize_from_attom_permits(permit_result.data)
            for p in permits:
                records.append(p.to_dict())
                sources.extend(p.sources)

    # 3. Zillow photos
    if address:
        zillow_result = await execute_apify_zillow_photos(
            payload={"address": address},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("apify_zillow")
        if zillow_result.outcome.value == "success" and zillow_result.data:
            photos = normalize_zillow_photos(zillow_result.data)
            for ph in photos:
                records.append(ph.to_dict())
                sources.extend(ph.sources)

    from aspire_orchestrator.services.adam.verifier import verify_records
    report = verify_records(records=records, sources=sources, required_fields=["normalized_address"])

    _emit_playbook_receipt(
        ctx=ctx,
        outcome_status="SUCCEEDED" if records else "FAILED",
        reason_code="EXECUTED" if records else "NO_DATA",
        playbook_name="PROPERTY_FACTS_AND_PERMITS",
        summary={
            "providers_called": providers_called,
            "record_count": len(records),
        },
    )

    return ResearchResponse(
        artifact_type="PropertyFactsAndPermits",
        summary=f"Property facts for {address or query}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        extra={"providers_called": providers_called},
    )


# ---------------------------------------------------------------------------
# ESTIMATE_RESEARCH playbook
# ---------------------------------------------------------------------------


async def execute_estimate_research(
    query: str,
    ctx: PlaybookContext,
    address: str = "",
) -> ResearchResponse:
    """ESTIMATE_RESEARCH — property context + HD pricing for estimate prep."""
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

    # 2. HD pricing
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
        extra={"providers_called": providers_called},
    )


# ---------------------------------------------------------------------------
# TOOL_MATERIAL_PRICE_CHECK playbook
# ---------------------------------------------------------------------------


@dataclass
class _StoreCandidate:
    store_id: str
    name: str
    address: str
    city: str
    state: str
    postal_code: str
    distance_miles: float | None = None


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
        city_match = _re.search(r"\bin\s+([A-Za-z][A-Za-z\s]+(?:,\s*[A-Za-z]{2})?)\\b", query)
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
            # (a) fuzzy address-hint auto-pick
            hint_lower = location_hint.lower()
            for cand in candidates:
                cand_addr = (cand.get("address") or "").lower()
                if hint_lower and any(
                    word in cand_addr for word in hint_lower.split() if len(word) > 3
                ):
                    store_id = str(cand.get("store_id", ""))
                    zip_code = zip_code or str(cand.get("postal_code", ""))
                    break
            # (b) haversine fallback (placeholder — office lat/lng not in ctx yet)
            if not store_id and candidates:
                store_id = str(candidates[0].get("store_id", ""))
                zip_code = zip_code or str(candidates[0].get("postal_code", ""))
        elif len(candidates) == 1:
            store_id = str(candidates[0].get("store_id", ""))
            zip_code = zip_code or str(candidates[0].get("postal_code", ""))
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
    # Search-level metadata captured from SerpAPI for refinable carousels
    final_taxonomy: list[dict[str, Any]] = []
    final_filters: list[dict[str, Any]] = []
    final_related_products: list[dict[str, Any]] = []
    final_pagination: dict[str, Any] = {}

    max_attempts = 1 if voice_path else 3
    hd_timeout = 4.0 if voice_path else 8.0

    async def _run_hd_search(attempt_query: str) -> Any:
        """Inner HD search helper. Returns ToolExecutionResult or Exception."""
        nonlocal providers_called

        # Guard: refuse to run when no store identity is resolvable (prevents
        # Bangor default-fallback poisoning the result with Maine inventory).
        resolved_store_id = store_id or ""

        if not resolved_store_id and not zip_code:
            # No store context at all — the SerpApi result will be poisoned
            # with the account-default Bangor/ME store. Refuse and let the
            # caller ask Ava to get the user's location.
            logger.warning(
                "TOOL_MATERIAL_PRICE_CHECK: no store_id or zip_code, refusing to run "
                "to avoid Bangor default-fallback (attempt_query=%s)",
                attempt_query[:80],
            )
            providers_called.append("store_unresolved")
            return None

        # Bug D fix: request the full SerpApi HD page size (24 products max).
        # Without this, SerpApi defaults to ~12 results. The completeness gate
        # (_product_missing_fields) then has 24 candidates to filter instead
        # of 12, raising the actual product count delivered to the frontend.
        hd_payload: dict[str, Any] = {"query": attempt_query, "hd_sort": "best_match", "num": "24"}
        if resolved_store_id:
            hd_payload["store_id"] = resolved_store_id
        if zip_code:
            hd_payload["delivery_zip"] = zip_code
        hd_result_inner = await execute_serpapi_homedepot_search(
            payload=hd_payload,
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
            timeout=hd_timeout,
        )
        # Fix 3 — persist adapter receipt immediately (Law #2).
        if hd_result_inner.receipt_data:
            try:
                from aspire_orchestrator.services.receipt_store import store_receipts
                store_receipts([hd_result_inner.receipt_data])
            except Exception:
                pass  # Receipt write failure must never block the playbook
        return hd_result_inner

    # Round 7 A.2 — SerpApi shopping with exponential backoff + jitter on 429.
    # Max 2 retries (3 total attempts), then graceful degrade to None so the
    # HD result still carries the response. Receipt for the rate-limited
    # outcome is emitted by the SerpApi adapter itself (Fix 4).
    async def _run_shopping_search(attempt_query: str) -> Any:
        """Inner Google Shopping search helper with retry. Returns result or None."""
        if voice_path:
            return None  # Voice path skips Shopping to stay within 5s budget
        for attempt in range(_SHOPPING_RETRY_MAX_ATTEMPTS + 1):
            try:
                result = await execute_serpapi_shopping_search(
                    payload={"query": attempt_query},
                    correlation_id=ctx.correlation_id,
                    suite_id=ctx.suite_id,
                    office_id=ctx.office_id,
                )
                if result.receipt_data:
                    try:
                        from aspire_orchestrator.services.receipt_store import store_receipts
                        store_receipts([result.receipt_data])
                    except Exception:
                        pass
                # On 429 at intermediate attempts, backoff + retry.
                if (
                    not result.outcome.value == "success"
                    and attempt < _SHOPPING_RETRY_MAX_ATTEMPTS
                    and result.error
                    and "rate_limited" in str(result.error).lower()
                ):
                    base_ms = _SHOPPING_RETRY_BASE_MS[min(attempt, len(_SHOPPING_RETRY_BASE_MS) - 1)]
                    jitter_ms = _random.randint(0, 100)
                    await asyncio.sleep((base_ms + jitter_ms) / 1000.0)
                    continue
                return result
            except Exception as exc:
                logger.warning("_run_shopping_search attempt %d error: %s", attempt, exc)
                if attempt < _SHOPPING_RETRY_MAX_ATTEMPTS:
                    await asyncio.sleep(0.25)
                    continue
                return None
        return None

    for attempt_idx in range(max_attempts):
        if attempt_idx == 0:
            attempt_query = query
        elif attempt_idx == 1:
            # Tighten: strip parentheticals, trailing qualifiers
            attempt_query = re.sub(r"\s*\([^)]*\)", "", query).strip()
            attempt_query = re.sub(r"\s+(for|with|to|and|or)\s+.*$", "", attempt_query, flags=re.IGNORECASE).strip()
        else:
            # Further tighten: first 3 words only
            words = query.split()
            attempt_query = " ".join(words[:3]) if len(words) > 3 else query

        # Run HD + Shopping concurrently (Shopping is None on voice path).
        hd_result, shopping_result = await asyncio.gather(
            _run_hd_search(attempt_query),
            _run_shopping_search(attempt_query),
            return_exceptions=True,
        )

        # If _run_hd_search returned None (store_unresolved guard fired),
        # break out immediately — no products, no store.
        if hd_result is None:
            break

        if isinstance(hd_result, Exception):
            logger.warning(
                "TOOL_MATERIAL_PRICE_CHECK attempt=%d HD error: %s",
                attempt_idx, hd_result,
            )
            providers_called.append("serpapi_home_depot_error")
            continue

        if isinstance(shopping_result, Exception):
            shopping_result = None

        providers_called.append("serpapi_home_depot")

        # Detect 429 on HD — don't log as provider error, surface via flag.
        hd_rate_limited = (
            hd_result.error is not None
            and "rate_limited" in str(hd_result.error).lower()
        )
        if hd_rate_limited:
            providers_called.append("serpapi_home_depot_rate_limited")

        # Check for SerpAPI account-default Bangor fallback poison
        hd_store_info: dict[str, Any] = {}
        hd_data: dict[str, Any] = {}
        if hd_result.outcome.value == "success" and hd_result.data:
            hd_data = hd_result.data
            store_data = hd_data.get("store", {})
            if store_data.get("default_store_fallback"):
                providers_called.append("serpapi_home_depot_default_fallback")
                logger.warning(
                    "TOOL_MATERIAL_PRICE_CHECK: Bangor default-fallback detected "
                    "(store=%s), skipping poisoned result",
                    store_data.get("store_id"),
                )
                continue

            # Build hd_store_info from SerpAPI search_information
            hd_store_info = {
                "store_id": store_data.get("store_id", ""),
                "store_name": store_data.get("store_name", ""),
                "name": store_data.get("store_name", ""),
                "address": "",
                "city": "",
                "state": "",
                "postal_code": zip_code or "",
                "phone": "",
                "website": "",
                "image_url": "",
                "open_now": None,
                "rating": None,
            }

            # Enrich from static directory when we have a store_id
            if hd_store_info["store_id"]:
                dir_record = lookup_store_by_id(hd_store_info["store_id"])
                if dir_record:
                    hd_store_info.update({
                        "address": dir_record.get("address", ""),
                        "city": dir_record.get("city", ""),
                        "state": dir_record.get("state", ""),
                        "postal_code": dir_record.get("postal_code", "") or zip_code or "",
                        "phone": dir_record.get("phone", ""),
                        "website": dir_record.get("website", ""),
                    })

        # Normalize products
        records: list[dict[str, Any]] = []
        sources: list[SourceAttribution] = []

        if hd_result.outcome.value == "success" and hd_data.get("results"):
            for item in hd_data["results"]:
                product = normalize_from_serpapi_homedepot(item)
                records.append(product.to_dict())
                sources.extend(product.sources)
            providers_called.append("serpapi_home_depot_success")

        # Google Shopping merge (text path only, skip on voice_path)
        if (
            not voice_path
            and shopping_result is not None
            and not isinstance(shopping_result, Exception)
            and shopping_result.outcome.value == "success"
            and shopping_result.data
        ):
            providers_called.append("serpapi_shopping")
            for item in shopping_result.data.get("results", [])[:6]:
                product = normalize_from_serpapi_shopping(item)
                records.append(product.to_dict())
                sources.extend(product.sources)

        # Filter products
        if include_other_stores:
            display_products = list(records)
        else:
            display_products = [r for r in records if r.get("retailer") == "Home Depot"]
        hd_products = [r for r in records if r.get("retailer") == "Home Depot"]
        complete_products = [r for r in display_products if not _product_missing_fields(r)]

        # Build store_summary card
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

        # Round 4: nearest_store overrides static-directory fields
        if nearest_store is not None:
            store_summary["name"] = nearest_store.name or store_summary["name"]
            store_summary["store_name"] = nearest_store.name or store_summary["store_name"]
            store_summary["address"] = nearest_store.address or store_summary["address"]
            store_summary["postal_code"] = nearest_store.postal_code or store_summary["postal_code"]
            parsed_city, parsed_state = _parse_city_state_from_formatted_address(
                nearest_store.address,
            )
            if parsed_city:
                store_summary["city"] = parsed_city
            if parsed_state:
                store_summary["state"] = parsed_state
            if nearest_store.photo_url:
                store_summary["image_url"] = nearest_store.photo_url
            if nearest_store.distance_miles is not None and nearest_store.distance_miles > 0:
                store_summary["distance_miles"] = round(nearest_store.distance_miles, 1)
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

            final_records = [store_summary, *complete_products]
            final_sources = sources
            final_store_summary = store_summary
            if not isinstance(hd_result, Exception) and hd_result.data:
                hd_data = hd_result.data
                final_taxonomy = list(hd_data.get("taxonomy") or [])[:6]
                final_filters = list(hd_data.get("filters") or [])[:12]
                final_related_products = list(hd_data.get("related_products") or [])[:8]
                final_pagination = hd_data.get("pagination") or {}
            break

    # Round 7 A.2 — decision flags
    nearest_distance: float | None = (
        round(nearest_store.distance_miles, 1)
        if (nearest_store is not None and nearest_store.distance_miles is not None)
        else None
    )
    if nearest_store is None and user_address and user_address.strip():
        hd_too_far = True
    elif nearest_distance is not None and nearest_distance > HD_TOO_FAR_MILES:
        hd_too_far = True
    else:
        hd_too_far = False

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
        if "serpapi_home_depot_rate_limited" in providers_called:
            reason_code = "shopping_429"
        elif (
            "store_unresolved" in providers_called
            or "serpapi_home_depot_default_fallback" in providers_called
        ):
            reason_code = "store_unresolved"
            decision_flags["store_unresolved"] = True
        elif hd_too_far:
            reason_code = "hd_too_far"
        elif not hd_has_stock and [r for r in providers_called if "home_depot" in r]:
            reason_code = "no_stock"
        else:
            reason_code = "missing_required_fields"

        _emit_playbook_receipt(
            ctx=ctx,
            outcome_status="FAILED",
            reason_code=reason_code,
            playbook_name="TOOL_MATERIAL_PRICE_CHECK",
            summary={
                "providers_called": providers_called,
                "missing_fields": last_missing_fields,
                "decision_flags": decision_flags,
            },
        )
        return ResearchResponse(
            artifact_type="ToolMaterialPriceCheck",
            summary=f"No complete results for: {query[:60]}",
            records=[],
            sources=[],
            freshness={"mode": "live"},
            confidence={"status": "failed", "score": 0.0},
            missing_fields=last_missing_fields,
            extra={
                "providers_called": providers_called,
                "decision_flags": decision_flags,
                "store_summary": final_store_summary,
                "taxonomy": final_taxonomy,
                "filters": final_filters,
                "related_products": final_related_products,
                "pagination": final_pagination,
            },
        )

    _emit_playbook_receipt(
        ctx=ctx,
        outcome_status="SUCCEEDED",
        reason_code="EXECUTED",
        playbook_name="TOOL_MATERIAL_PRICE_CHECK",
        summary={
            "providers_called": providers_called,
            "product_count": len(final_records) - 1,  # exclude store_summary
            "decision_flags": decision_flags,
        },
    )

    return ResearchResponse(
        artifact_type="ToolMaterialPriceCheck",
        summary=f"Price check for: {query[:60]}",
        records=final_records,
        sources=final_sources,
        freshness={"mode": "live"},
        confidence={"status": "complete", "score": 0.9},
        missing_fields=[],
        extra={
            "providers_called": providers_called,
            "decision_flags": decision_flags,
            "store_summary": final_store_summary,
            "taxonomy": final_taxonomy,
            "filters": final_filters,
            "related_products": final_related_products,
            "pagination": final_pagination,
        },
    )


# ---------------------------------------------------------------------------
# COMPETITOR_PRICING_SCAN playbook
# ---------------------------------------------------------------------------


async def execute_competitor_pricing_scan(
    query: str,
    ctx: PlaybookContext,
    address: str = "",
) -> ResearchResponse:
    """COMPETITOR_PRICING_SCAN — web + Exa for competitor pricing intel."""
    from aspire_orchestrator.providers.exa_client import execute_exa_search

    logger.info("Executing COMPETITOR_PRICING_SCAN for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    result = await execute_exa_search(
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

    if result.outcome.value == "success" and result.data:
        for item in (result.data.get("results") or [])[:5]:
            records.append({
                "card_kind": "web_evidence",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("text", "")[:500],
                "published_date": item.get("published_date"),
                "retailer": "web",
            })
            sources.append(SourceAttribution(
                provider="exa",
                retrieved_at=datetime.now(timezone.utc).isoformat(),
            ))

    _emit_playbook_receipt(
        ctx=ctx,
        outcome_status="SUCCEEDED" if records else "FAILED",
        reason_code="EXECUTED" if records else "NO_DATA",
        playbook_name="COMPETITOR_PRICING_SCAN",
        summary={"providers_called": providers_called, "record_count": len(records)},
    )

    return ResearchResponse(
        artifact_type="CompetitorPricingScan",
        summary=f"Competitor pricing for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": "complete" if records else "failed", "score": 0.7 if records else 0.0},
        missing_fields=[],
        extra={"providers_called": providers_called},
    )


# ---------------------------------------------------------------------------
# SUBCONTRACTOR_SCOUT playbook
# ---------------------------------------------------------------------------


async def execute_subcontractor_scout(
    query: str,
    ctx: PlaybookContext,
    location: str = "",
) -> ResearchResponse:
    """SUBCONTRACTOR_SCOUT — Yelp for local subcontractor discovery."""
    from aspire_orchestrator.providers.serpapi_yelp_client import execute_serpapi_yelp_search

    logger.info("Executing SUBCONTRACTOR_SCOUT for: %s location=%r", query[:80], location[:40])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    yelp_result = await execute_serpapi_yelp_search(
        payload={
            "find_desc": query,
            "find_loc": location or "",
        },
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
        timeout=5.0,
    )
    providers_called.append("yelp")

    if yelp_result.outcome.value == "success" and yelp_result.data:
        for supplier in (yelp_result.data.get("suppliers") or [])[:10]:
            records.append({
                "card_kind": "supplier",
                **supplier,
            })
            sources.append(SourceAttribution(
                provider="yelp",
                retrieved_at=datetime.now(timezone.utc).isoformat(),
            ))

    _emit_playbook_receipt(
        ctx=ctx,
        outcome_status="SUCCEEDED" if records else "FAILED",
        reason_code="EXECUTED" if records else "NO_DATA",
        playbook_name="SUBCONTRACTOR_SCOUT",
        summary={"providers_called": providers_called, "record_count": len(records)},
    )

    return ResearchResponse(
        artifact_type="SubcontractorScout",
        summary=f"Subcontractor scout for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": "complete" if records else "failed", "score": 0.75 if records else 0.0},
        missing_fields=[],
        extra={"providers_called": providers_called},
    )


# ---------------------------------------------------------------------------
# TERRITORY_OPPORTUNITY_SCAN playbook
# ---------------------------------------------------------------------------


async def execute_territory_opportunity_scan(
    query: str,
    ctx: PlaybookContext,
    geo_scope: str = "",
) -> ResearchResponse:
    """TERRITORY_OPPORTUNITY_SCAN — Exa market intel for territory planning."""
    from aspire_orchestrator.providers.exa_client import execute_exa_search

    logger.info("Executing TERRITORY_OPPORTUNITY_SCAN for: %s geo=%r", query[:80], geo_scope[:40])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Exa for market intelligence
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
        for item in (exa_result.data.get("results") or [])[:5]:
            records.append({
                "card_kind": "web_evidence",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("text", "")[:500],
                "published_date": item.get("published_date"),
                "retailer": "web",
            })
            sources.append(SourceAttribution(
                provider="exa",
                retrieved_at=datetime.now(timezone.utc).isoformat(),
            ))

    _emit_playbook_receipt(
        ctx=ctx,
        outcome_status="SUCCEEDED" if records else "FAILED",
        reason_code="EXECUTED" if records else "NO_DATA",
        playbook_name="TERRITORY_OPPORTUNITY_SCAN",
        summary={"providers_called": providers_called, "record_count": len(records)},
    )

    return ResearchResponse(
        artifact_type="TerritoryOpportunityScan",
        summary=f"Territory scan for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": "complete" if records else "failed", "score": 0.65 if records else 0.0},
        missing_fields=[],
        extra={"providers_called": providers_called},
    )
