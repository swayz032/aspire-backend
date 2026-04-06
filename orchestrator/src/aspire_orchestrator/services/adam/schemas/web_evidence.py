"""WebEvidence — Canonical schema for web search results / extracted content.

Sources: Brave, Exa, Tavily, Parallel
Trust class C — web extraction tier.
Exa grounding.confidence (low/medium/high) feeds directly into verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WebEvidence:
    """Canonical web evidence record."""

    url: str = ""
    title: str = ""
    snippet: str = ""
    content: str = ""
    domain: str = ""
    published_date: str = ""
    retrieved_at: str = ""
    provider: str = ""
    relevance_score: float = 0.0
    confidence: float = 0.0  # 0-1, from Exa grounding or estimated
    exa_grounding_confidence: str = ""  # "low" | "medium" | "high" (Exa native)
    summary: str = ""  # Exa per-result summary
    highlights: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "extra"}
        d.update(self.extra)
        return d
