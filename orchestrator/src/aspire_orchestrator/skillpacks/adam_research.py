"""Adam Research Skill Pack — Web search, places, vendor comparison, RFQ generation.

All GREEN tier (no approval needed). Uses the search_router for cascading
provider fallback (brave -> tavily for web, google_places -> ... for places).

Phase 3 W3: Enhanced with LLM-powered reasoning via EnhancedSkillPack base.
  - plan_search: LLM plans search strategy before executing
  - verify_evidence: LLM verifies and scores search results
  - generate_outreach_packet: LLM generates vendor outreach documents
  - Model routing: cheap_classifier (GPT-5-mini) for queries, primary_reasoner (GPT-5.2) for synthesis

Law compliance:
  - Law #1: Skill pack orchestrates tool calls; orchestrator decides when to invoke.
  - Law #2: Every method emits a receipt via _emit_receipt / emit_receipt.
  - Law #3: Fails closed on missing query or provider errors.
  - Law #7: Delegates to tool_executor/search_router — no autonomous decisions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.services.search_router import (
    route_web_search,
    route_places_search,
)
from aspire_orchestrator.services.tool_executor import execute_tool
from aspire_orchestrator.services.browser_service import (
    get_browser_service,
    DomainDeniedError,
    NavigationTimeoutError,
    ScreenshotUploadError,
)

logger = logging.getLogger(__name__)

# Wave 5: Activity Event Callback for Canvas Chat Mode streaming
_activity_event_callback: callable | None = None


def set_activity_event_callback(callback: callable | None) -> None:
    """Set global callback for emitting agent activity events (Wave 5)."""
    global _activity_event_callback
    _activity_event_callback = callback


def get_activity_event_callback() -> callable | None:
    """Return the current global activity event callback (if set)."""
    return _activity_event_callback


def _emit_activity_event(event_type: str, message: str, icon: str = "info") -> None:
    """Emit activity event to Canvas Chat Mode stream (Wave 5)."""
    if _activity_event_callback:
        _activity_event_callback({
            "type": event_type,
            "message": message,
            "icon": icon,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "agent": "adam",
        })

ACTOR_ADAM = "skillpack:adam-research"
RECEIPT_VERSION = "1.0"


@dataclass(frozen=True)
class SkillPackResult:
    """Result from an Adam Research skill pack method."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class AdamResearchContext:
    """Required context for all Adam Research operations."""

    suite_id: str
    office_id: str
    correlation_id: str
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """Compute SHA256 hash of inputs for receipt linkage."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _emit_receipt(
    *,
    ctx: AdamResearchContext,
    event_type: str,
    status: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for an Adam Research operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_ADAM,
        "correlation_id": ctx.correlation_id,
        "status": status,
        "inputs_hash": _compute_inputs_hash(inputs),
        "policy": {
            "decision": "allow",
            "policy_id": "adam-research-v1",
            "reasons": [],
        },
        "redactions": [],
    }
    if metadata:
        receipt["metadata"] = metadata
    return receipt


class AdamResearchSkillPack:
    async def research_search(
        self,
        query: str,
        context: AdamResearchContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.search_web(query=query, context=context)

    """Adam Research skill pack — web search, places, vendor comparison, RFQ."""

    async def search_web(
        self,
        query: str,
        context: AdamResearchContext,
    ) -> SkillPackResult:
        """Search the web via search_router (brave -> tavily fallback).

        GREEN tier, no approval required.
        """
        if not query or not query.strip():
            receipt = _emit_receipt(
                ctx=context,
                event_type="research.search",
                status="denied",
                inputs={"action": "research.search", "query": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_QUERY"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: query",
            )

        # Wave 5: Emit "thinking" event
        _emit_activity_event("thinking", f"Searching web for: {query.strip()}", "search")

        # Wave 5: Emit "tool_call" event
        _emit_activity_event("tool_call", "Calling Brave Search API", "code")

        result: ToolExecutionResult = await route_web_search(
            payload={"query": query.strip()},
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        # Wave 5: Emit "step" event with result count
        if result.outcome == Outcome.SUCCESS:
            result_count = len(result.data.get("results", []))
            _emit_activity_event("step", f"Found {result_count} results, ranking by relevance", "list")

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _emit_receipt(
            ctx=context,
            event_type="research.search",
            status=status,
            inputs={"action": "research.search", "query": query.strip()},
            metadata={
                "provider_used": result.data.get("provider_used"),
                "fallback_chain": result.data.get("fallback_chain", []),
                "tool_id": result.tool_id,
            },
        )

        # Wave 5: Emit "done" event
        if result.outcome == Outcome.SUCCESS:
            _emit_activity_event("done", "Research complete", "checkmark")
        else:
            _emit_activity_event("error", f"Research failed: {result.error}", "error")

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def research_places(
        self,
        query: str,
        location: str | None,
        context: AdamResearchContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.search_places(query=query, location=location, context=context)

    async def search_places(
        self,
        query: str,
        location: str | None,
        context: AdamResearchContext,
    ) -> SkillPackResult:
        """Search local businesses via search_router places chain.

        GREEN tier, no approval required.
        """
        if not query or not query.strip():
            receipt = _emit_receipt(
                ctx=context,
                event_type="research.places",
                status="denied",
                inputs={"action": "research.places", "query": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_QUERY"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: query",
            )

        payload: dict[str, Any] = {"query": query.strip()}
        if location:
            payload["location"] = location.strip()

        result: ToolExecutionResult = await route_places_search(
            payload=payload,
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _emit_receipt(
            ctx=context,
            event_type="research.places",
            status=status,
            inputs={"action": "research.places", "query": query.strip(), "location": location or ""},
            metadata={
                "provider_used": result.data.get("provider_used"),
                "fallback_chain": result.data.get("fallback_chain", []),
                "tool_id": result.tool_id,
            },
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def compare_vendors(
        self,
        criteria: dict[str, Any],
        context: AdamResearchContext,
    ) -> SkillPackResult:
        """Compare vendors by running multiple searches and ranking results.

        GREEN tier. Searches web + places for each vendor category,
        then scores and ranks by relevance to criteria.
        """
        search_query = criteria.get("query", "")
        categories = criteria.get("categories", [])
        location = criteria.get("location")

        if not search_query:
            receipt = _emit_receipt(
                ctx=context,
                event_type="research.compare",
                status="denied",
                inputs={"action": "research.compare", "criteria": criteria},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_QUERY"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required criteria.query for vendor comparison",
            )

        # Multi-search: web search for general info + places for local results
        web_result = await route_web_search(
            payload={"query": search_query},
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        places_result: ToolExecutionResult | None = None
        if location:
            places_result = await route_places_search(
                payload={"query": search_query, "location": location},
                correlation_id=context.correlation_id,
                suite_id=context.suite_id,
                office_id=context.office_id,
                risk_tier="green",
                capability_token_id=context.capability_token_id,
                capability_token_hash=context.capability_token_hash,
            )

        # Build comparison data from results
        comparison = _build_comparison(
            web_result=web_result,
            places_result=places_result,
            categories=categories,
            query=search_query,
        )

        any_success = (
            web_result.outcome == Outcome.SUCCESS
            or (places_result is not None and places_result.outcome == Outcome.SUCCESS)
        )
        status = "ok" if any_success else "failed"

        receipt = _emit_receipt(
            ctx=context,
            event_type="research.compare",
            status=status,
            inputs={"action": "research.compare", "criteria": criteria},
            metadata={
                "web_provider": web_result.data.get("provider_used"),
                "places_provider": places_result.data.get("provider_used") if places_result else None,
                "vendor_count": len(comparison.get("vendors", [])),
            },
        )

        return SkillPackResult(
            success=any_success,
            data=comparison,
            receipt=receipt,
            error=None if any_success else "All search providers failed",
        )

    async def research_compare(
        self,
        criteria: dict[str, Any],
        context: AdamResearchContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.compare_vendors(criteria=criteria, context=context)

    async def generate_rfq(
        self,
        vendor_data: dict[str, Any],
        requirements: dict[str, Any],
        context: AdamResearchContext,
    ) -> SkillPackResult:
        """Generate a Request for Quotation document from vendor data + requirements.

        GREEN tier. Template-based document generation, no external calls.
        """
        vendor_name = vendor_data.get("name", "")
        if not vendor_name:
            receipt = _emit_receipt(
                ctx=context,
                event_type="research.rfq",
                status="denied",
                inputs={"action": "research.rfq", "vendor_name": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_VENDOR_NAME"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required vendor_data.name for RFQ generation",
            )

        rfq_document = _build_rfq_document(vendor_data, requirements, context)

        receipt = _emit_receipt(
            ctx=context,
            event_type="research.rfq",
            status="ok",
            inputs={
                "action": "research.rfq",
                "vendor_name": vendor_name,
                "requirement_count": len(requirements.get("items", [])),
            },
            metadata={
                "rfq_id": rfq_document["rfq_id"],
                "vendor_name": vendor_name,
            },
        )

        return SkillPackResult(
            success=True,
            data=rfq_document,
            receipt=receipt,
        )

    async def research_rfq(
        self,
        vendor_data: dict[str, Any],
        requirements: dict[str, Any],
        context: AdamResearchContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.generate_rfq(
            vendor_data=vendor_data,
            requirements=requirements,
            context=context,
        )


def _build_comparison(
    *,
    web_result: ToolExecutionResult,
    places_result: ToolExecutionResult | None,
    categories: list[str],
    query: str,
) -> dict[str, Any]:
    """Build a structured vendor comparison from search results."""
    vendors: list[dict[str, Any]] = []
    sources: list[str] = []

    if web_result.outcome == Outcome.SUCCESS and web_result.data:
        sources.append("web")
        results = web_result.data.get("results", [])
        for i, r in enumerate(results[:10]):
            vendors.append({
                "name": r.get("title", r.get("name", f"Result {i + 1}")),
                "source": "web",
                "url": r.get("url", ""),
                "snippet": r.get("snippet", r.get("description", "")),
                "relevance_score": max(0.0, 1.0 - (i * 0.1)),
            })

    if places_result and places_result.outcome == Outcome.SUCCESS and places_result.data:
        sources.append("places")
        results = places_result.data.get("results", [])
        for i, r in enumerate(results[:10]):
            vendors.append({
                "name": r.get("name", f"Place {i + 1}"),
                "source": "places",
                "address": r.get("address", ""),
                "rating": r.get("rating"),
                "relevance_score": max(0.0, 1.0 - (i * 0.1)),
            })

    # Sort by relevance score descending
    vendors.sort(key=lambda v: v.get("relevance_score", 0), reverse=True)

    return {
        "query": query,
        "categories": categories,
        "vendors": vendors,
        "sources": sources,
        "total_results": len(vendors),
    }


def _build_rfq_document(
    vendor_data: dict[str, Any],
    requirements: dict[str, Any],
    context: AdamResearchContext,
) -> dict[str, Any]:
    """Build a structured RFQ document from vendor data and requirements."""
    rfq_id = f"RFQ-{uuid.uuid4().hex[:8].upper()}"

    return {
        "rfq_id": rfq_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite_id": context.suite_id,
        "office_id": context.office_id,
        "vendor": {
            "name": vendor_data.get("name", ""),
            "contact": vendor_data.get("contact", ""),
            "address": vendor_data.get("address", ""),
            "url": vendor_data.get("url", ""),
        },
        "requirements": {
            "title": requirements.get("title", "Request for Quotation"),
            "description": requirements.get("description", ""),
            "items": requirements.get("items", []),
            "deadline": requirements.get("deadline", ""),
            "budget_range": requirements.get("budget_range", ""),
        },
        "terms": {
            "response_deadline": requirements.get("response_deadline", ""),
            "delivery_terms": requirements.get("delivery_terms", ""),
            "payment_terms": requirements.get("payment_terms", ""),
        },
        "status": "draft",
    }


# =============================================================================
# Phase 3 W3: Enhanced Adam Research with LLM reasoning
# =============================================================================

from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class EnhancedAdamResearch(AgenticSkillPack):
    """LLM-enhanced Adam Research — search planning, evidence verification, outreach.

    Extends the rule-based AdamResearchSkillPack with LLM reasoning:
    - plan_search: GPT-5-mini classifies search intent, builds multi-query plan
    - verify_evidence: GPT-5.2 evaluates search results for relevance/credibility
    - generate_outreach_packet: GPT-5.2 generates structured vendor outreach documents

    All operations remain GREEN tier. The LLM adds intelligence but doesn't
    change the risk classification — no approvals needed, no external side effects.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="adam-research",
            agent_name="Adam Research",
            default_risk_tier="green",
            memory_enabled=True,
        )
        self._rule_pack = AdamResearchSkillPack()

    async def plan_search(
        self,
        user_request: str,
        ctx: AgentContext,
    ) -> AgentResult:
        """Plan a multi-query search strategy using LLM classification.

        Uses cheap_classifier (GPT-5-mini) to:
        1. Classify intent (web_search, places_search, comparison, rfq)
        2. Generate optimized search queries
        3. Suggest provider routing preferences

        GREEN tier — planning only, no execution.
        """
        if not user_request or not user_request.strip():
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.plan",
                status="failed",
                inputs={"user_request": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUEST"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing user_request")

        return await self.execute_with_llm(
            prompt=(
                f"You are Adam, the research specialist for a small business.\n"
                f"The user wants: {user_request}\n\n"
                f"Create a search plan with:\n"
                f"1. Intent classification (web_search, places_search, comparison, rfq)\n"
                f"2. Optimized search queries (max 3)\n"
                f"3. Provider preference (brave, tavily, google_places, etc.)\n"
                f"4. Expected result format\n\n"
                f"Return a structured JSON plan."
            ),
            ctx=ctx,
            event_type="research.plan",
            step_type="classify",
            inputs={"action": "research.plan", "user_request": user_request.strip()},
        )

    async def verify_evidence(
        self,
        search_results: list[dict[str, Any]],
        original_query: str,
        ctx: AgentContext,
    ) -> AgentResult:
        """Verify and score search results using LLM analysis.

        Uses primary_reasoner (GPT-5.2) to:
        1. Evaluate relevance to original query
        2. Check source credibility
        3. Identify contradictions across sources
        4. Generate confidence scores

        GREEN tier — analysis only, no state changes.
        """
        if not search_results:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.verify",
                status="failed",
                inputs={"original_query": original_query, "result_count": 0},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["NO_RESULTS_TO_VERIFY"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="No search results to verify")

        # Truncate results for LLM context (avoid token overflow)
        truncated = [
            {k: v for k, v in r.items() if k in ("name", "title", "snippet", "url", "source", "rating")}
            for r in search_results[:10]
        ]

        return await self.execute_with_llm(
            prompt=(
                f"You are Adam, verifying search results for a small business owner.\n"
                f"Original query: {original_query}\n\n"
                f"Results to verify:\n{json.dumps(truncated, indent=2)}\n\n"
                f"For each result, assess:\n"
                f"1. Relevance to query (0.0-1.0)\n"
                f"2. Source credibility (high/medium/low)\n"
                f"3. Any red flags or contradictions\n\n"
                f"Return a structured verification report."
            ),
            ctx=ctx,
            event_type="research.verify",
            step_type="verify",
            inputs={
                "action": "research.verify",
                "original_query": original_query,
                "result_count": len(search_results),
            },
        )

    async def generate_outreach_packet(
        self,
        vendor_data: dict[str, Any],
        business_context: dict[str, Any],
        ctx: AgentContext,
    ) -> AgentResult:
        """Generate a structured vendor outreach document using LLM.

        Uses primary_reasoner (GPT-5.2) to:
        1. Draft professional outreach message
        2. Tailor to vendor's service category
        3. Include relevant business requirements
        4. Format as structured packet (not email — Eli handles sending)

        GREEN tier — document generation, no external communication.
        """
        vendor_name = vendor_data.get("name", "")
        if not vendor_name:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.outreach_packet",
                status="failed",
                inputs={"vendor_name": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_VENDOR_NAME"]
            await self.emit_receipt(receipt)
            return AgentResult(
                success=False, receipt=receipt, error="Missing vendor_data.name",
            )

        return await self.execute_with_llm(
            prompt=(
                f"You are Adam, preparing a vendor outreach packet.\n\n"
                f"Vendor: {vendor_name}\n"
                f"Service category: {vendor_data.get('category', 'general')}\n"
                f"Business name: {business_context.get('business_name', 'Our Business')}\n"
                f"Requirements: {business_context.get('requirements', 'General inquiry')}\n\n"
                f"Generate a structured outreach packet with:\n"
                f"1. Subject line\n"
                f"2. Professional introduction\n"
                f"3. Specific requirements/questions\n"
                f"4. Requested timeline\n"
                f"5. Call to action\n\n"
                f"This is a DRAFT PACKET — it will be reviewed by the user\n"
                f"and sent by Eli (inbox specialist) if approved."
            ),
            ctx=ctx,
            event_type="research.outreach_packet",
            step_type="draft",
            inputs={
                "action": "research.outreach_packet",
                "vendor_name": vendor_name,
                "business_name": business_context.get("business_name", ""),
            },
        )

    async def browser_navigate(
        self,
        url: str,
        ctx: AgentContext,
    ) -> AgentResult:
        """Navigate browser to URL and capture screenshot (Hybrid Browser View — Wave 2).

        Uses browser_service.py for Playwright-based screenshot capture.
        Emits browser_screenshot SSE event for real-time Canvas Mode display.

        YELLOW tier — external site visit requires user approval.

        Args:
            url: Target URL (must pass domain allowlist)
            ctx: Agent execution context

        Returns:
            AgentResult with screenshot URL and metadata

        Law compliance:
            - Law #2: Emits receipt for all navigation attempts (success/deny/fail)
            - Law #3: Fails closed on invalid URL (DomainDeniedError)
            - Law #4: YELLOW risk tier (user approval required before execution)
            - Law #9: Domain allowlist enforced, PII redacted from page_url/page_title
        """
        # Validate inputs (Law #3: fail closed)
        if not url or not url.strip():
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.browser_navigate",
                status="denied",
                inputs={"action": "research.browser_navigate", "url": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_URL"]
            await self.emit_receipt(receipt)
            return AgentResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: url",
            )

        screenshot_id = str(uuid.uuid4())
        url_clean = url.strip()

        # Emit activity event: thinking
        _emit_activity_event(
            event_type="thinking",
            message=f"Navigating browser to {url_clean}...",
            icon="browser",
        )

        try:
            # Get browser service singleton
            browser_service = get_browser_service()

            # Emit activity event: tool_call
            _emit_activity_event(
                event_type="tool_call",
                message=f"Launching Playwright browser (screenshot_id: {screenshot_id[:8]}...)",
                icon="code",
            )

            # Navigate and capture screenshot
            screenshot_result = await browser_service.navigate_and_screenshot(
                url=url_clean,
                screenshot_id=screenshot_id,
                suite_id=ctx.suite_id,
                viewport_width=1280,
                viewport_height=800,
            )

            # Emit browser_screenshot SSE event for Canvas Mode (Wave 3)
            if _activity_event_callback:
                _activity_event_callback({
                    "type": "browser_screenshot",
                    "screenshot_url": screenshot_result.screenshot_url,
                    "screenshot_id": screenshot_result.screenshot_id,
                    "page_url": screenshot_result.page_url,
                    "page_title": screenshot_result.page_title,
                    "viewport_width": screenshot_result.viewport_width,
                    "viewport_height": screenshot_result.viewport_height,
                    "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                    "agent": "adam",
                })

            # Emit activity event: done
            _emit_activity_event(
                event_type="done",
                message=f"Screenshot captured: {screenshot_result.page_title}",
                icon="checkmark",
            )

            # Build receipt (Law #2)
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.browser_navigate",
                status="ok",
                inputs={
                    "action": "research.browser_navigate",
                    "url": screenshot_result.page_url,  # Redacted URL (no query params)
                },
                metadata={
                    "screenshot_id": screenshot_result.screenshot_id,
                    "screenshot_url": screenshot_result.screenshot_url,
                    "page_title": screenshot_result.page_title,  # PII-redacted
                    "viewport_width": screenshot_result.viewport_width,
                    "viewport_height": screenshot_result.viewport_height,
                    "page_load_time_ms": screenshot_result.page_load_time_ms,
                },
            )
            await self.emit_receipt(receipt)

            return AgentResult(
                success=True,
                data={
                    "screenshot_id": screenshot_result.screenshot_id,
                    "screenshot_url": screenshot_result.screenshot_url,
                    "page_url": screenshot_result.page_url,
                    "page_title": screenshot_result.page_title,
                    "page_load_time_ms": screenshot_result.page_load_time_ms,
                },
                receipt=receipt,
            )

        except DomainDeniedError as e:
            # Domain not in allowlist (SSRF blocked)
            logger.warning(f"Browser navigation denied: {e}", extra={"url": url_clean})

            _emit_activity_event(
                event_type="error",
                message=f"Navigation denied: {str(e)}",
                icon="alert",
            )

            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.browser_navigate",
                status="denied",
                inputs={"action": "research.browser_navigate", "url": url_clean},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["DOMAIN_NOT_ALLOWED"]
            receipt["policy"]["details"] = str(e)
            await self.emit_receipt(receipt)

            return AgentResult(
                success=False,
                receipt=receipt,
                error=f"Domain denied: {str(e)}",
            )

        except NavigationTimeoutError as e:
            # Page load timeout (>30s)
            logger.error(f"Browser navigation timeout: {e}", extra={"url": url_clean})

            _emit_activity_event(
                event_type="error",
                message=f"Navigation timeout: {str(e)}",
                icon="alert",
            )

            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.browser_navigate",
                status="failed",
                inputs={"action": "research.browser_navigate", "url": url_clean},
            )
            receipt["policy"]["decision"] = "allow"
            receipt["policy"]["failure_reason"] = "TIMEOUT"
            await self.emit_receipt(receipt)

            return AgentResult(
                success=False,
                receipt=receipt,
                error=f"Navigation timeout: {str(e)}",
            )

        except ScreenshotUploadError as e:
            # S3 upload failed
            logger.error(f"Screenshot upload failed: {e}", extra={"url": url_clean})

            _emit_activity_event(
                event_type="error",
                message=f"Screenshot upload failed: {str(e)}",
                icon="alert",
            )

            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.browser_navigate",
                status="failed",
                inputs={"action": "research.browser_navigate", "url": url_clean},
            )
            receipt["policy"]["decision"] = "allow"
            receipt["policy"]["failure_reason"] = "UPLOAD_FAILED"
            await self.emit_receipt(receipt)

            return AgentResult(
                success=False,
                receipt=receipt,
                error=f"Screenshot upload failed: {str(e)}",
            )

        except Exception as e:
            # Unexpected error (catch-all)
            logger.error(
                f"Browser navigation failed: {e}",
                exc_info=True,
                extra={"url": url_clean}
            )

            _emit_activity_event(
                event_type="error",
                message=f"Navigation failed: {str(e)}",
                icon="alert",
            )

            receipt = self.build_receipt(
                ctx=ctx,
                event_type="research.browser_navigate",
                status="failed",
                inputs={"action": "research.browser_navigate", "url": url_clean},
            )
            receipt["policy"]["decision"] = "allow"
            receipt["policy"]["failure_reason"] = "UNEXPECTED_ERROR"
            await self.emit_receipt(receipt)

            return AgentResult(
                success=False,
                receipt=receipt,
                error=f"Unexpected error: {str(e)}",
            )
