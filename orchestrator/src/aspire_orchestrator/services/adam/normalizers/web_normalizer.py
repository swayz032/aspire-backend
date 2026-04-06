"""Normalize web search provider responses to WebEvidence.

Handles: Brave, Exa, Tavily, Parallel
Exa grounding.confidence is preserved as exa_grounding_confidence for
direct use by the verifier.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from aspire_orchestrator.services.adam.schemas.web_evidence import WebEvidence


def normalize_from_brave(data: dict[str, Any]) -> WebEvidence:
    """Normalize a Brave search result to WebEvidence."""
    url = data.get("url", "")
    return WebEvidence(
        url=url,
        title=data.get("title", ""),
        snippet=data.get("description", ""),
        domain=urlparse(url).netloc if url else "",
        published_date=data.get("age", ""),
        provider="brave",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )


def normalize_from_exa(data: dict[str, Any], grounding: dict[str, Any] | None = None) -> WebEvidence:
    """Normalize an Exa search result to WebEvidence.

    If grounding data is available (from outputSchema response), the
    field-level confidence is preserved for the verifier.
    """
    url = data.get("url", "")
    confidence_str = ""
    confidence_score = 0.0

    if grounding:
        confidence_str = grounding.get("confidence", "")
        confidence_map = {"high": 0.90, "medium": 0.70, "low": 0.40}
        confidence_score = confidence_map.get(confidence_str, 0.0)

    return WebEvidence(
        url=url,
        title=data.get("title", ""),
        snippet="",
        content=data.get("text", ""),
        domain=urlparse(url).netloc if url else "",
        published_date=data.get("publishedDate", "") or data.get("published_date", ""),
        provider="exa",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        relevance_score=0.0,
        confidence=confidence_score,
        exa_grounding_confidence=confidence_str,
        summary=data.get("summary", ""),
        highlights=data.get("highlights", []),
    )


def normalize_from_tavily(data: dict[str, Any]) -> WebEvidence:
    """Normalize a Tavily search result to WebEvidence."""
    url = data.get("url", "")
    return WebEvidence(
        url=url,
        title=data.get("title", ""),
        snippet=data.get("content", "")[:500],
        content=data.get("content", ""),
        domain=urlparse(url).netloc if url else "",
        provider="tavily",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        relevance_score=data.get("score", 0.0),
    )


def normalize_from_parallel(data: dict[str, Any]) -> WebEvidence:
    """Normalize a Parallel search result to WebEvidence."""
    url = data.get("url", "")
    return WebEvidence(
        url=url,
        title=data.get("title", ""),
        snippet=data.get("excerpt", ""),
        domain=data.get("source_domain", "") or (urlparse(url).netloc if url else ""),
        published_date=data.get("published_date", ""),
        provider="parallel",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
