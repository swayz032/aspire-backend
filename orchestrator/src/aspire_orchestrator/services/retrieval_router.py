"""Retrieval Router — Agentic cross-domain RAG routing.

Sits in the orchestrator (Law #1: Single Brain) and determines which
domain RAG services to query for a given user utterance. Executes
parallel retrieval across relevant domains, assembles unified context.

This is NOT an autonomous agent. It's a deterministic router with
keyword-based domain detection. The orchestrator controls it.

Architecture:
  - Each agent has primary knowledge domains (from manifests)
  - Ava gets cross-domain routing based on query analysis
  - Retrieval is parallel across domains for low latency
  - Results are assembled into a single context string

Law compliance:
  - Law #1: Orchestrator-controlled (no autonomous decisions)
  - Law #2: Generates retrieval receipt
  - Law #3: Fail-closed (empty context on failure, not guesses)
  - Law #6: Suite-scoped (all services enforce RLS)
  - Law #7: Router is a hand (routes, doesn't decide)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)

# Domain → service mapping
_DOMAIN_SERVICE_MAP: dict[str, str] = {
    "legal": "legal",
    "finance": "finance",
    "general": "general",
    "communication": "communication",
}

# Agent → primary domain mapping (from skill_pack_manifests.yaml knowledge_domains)
_AGENT_DOMAINS: dict[str, list[str]] = {
    "ava": ["general"],
    "finn": ["finance"],
    "clara": ["legal"],
    "eli": ["communication"],
    "adam": ["general"],
    "quinn": ["finance"],
    "teressa": ["finance"],
    "milo": ["finance"],
    "nora": [],
    "sarah": [],
    "tec": [],
    "mail_ops": [],
}

# Keywords that signal cross-domain retrieval needs
_FINANCE_SIGNALS = {
    "tax", "invoice", "payment", "cash flow", "deduction", "revenue",
    "expense", "budget", "profit", "loss", "financial", "money",
    "accounting", "bookkeeping", "payroll", "irs", "quarterly",
}
_LEGAL_SIGNALS = {
    "contract", "agreement", "clause", "nda", "liability", "legal",
    "compliance", "sign", "signature", "terms", "indemnification",
    "dispute", "warranty", "lease", "subcontractor",
}
_COMMUNICATION_SIGNALS = {
    "email", "draft", "send", "reply", "follow-up", "followup",
    "message", "inbox", "subject line", "tone", "writing",
    "communication", "outreach", "correspondence",
}
_GENERAL_SIGNALS = {
    "aspire", "platform", "how does", "what is", "best practice",
    "business", "operations", "industry", "productivity", "workflow",
    "scheduling", "meeting", "calendar", "team", "management",
}


@dataclass
class RetrievalRouterResult:
    """Result from cross-domain retrieval routing."""

    context: str = ""
    domains_queried: list[str] = field(default_factory=list)
    total_chunks: int = 0
    timing_ms: float = 0.0
    receipt_id: str = ""


class RetrievalRouter:
    """Agentic retrieval routing — determines which knowledge domains
    are needed for a query and executes parallel retrieval.

    Thread-safe for concurrent async usage.
    """

    def _determine_domains(self, agent_id: str, query: str) -> list[str]:
        """Determine which RAG domains to query.

        For the target agent: always query their primary domain.
        For Ava: analyze query to determine cross-domain needs.
        For agents without domains: skip RAG entirely.
        """
        # Start with agent's own domains
        agent_domains = list(_AGENT_DOMAINS.get(agent_id, []))

        # If agent has no domains, check if query warrants cross-domain
        if not agent_domains and agent_id not in ("ava",):
            return []

        # Cross-domain routing for Ava (orchestrator) and general queries
        if agent_id == "ava" or not agent_domains:
            q_lower = query.lower()
            if any(kw in q_lower for kw in _FINANCE_SIGNALS):
                if "finance" not in agent_domains:
                    agent_domains.append("finance")
            if any(kw in q_lower for kw in _LEGAL_SIGNALS):
                if "legal" not in agent_domains:
                    agent_domains.append("legal")
            if any(kw in q_lower for kw in _COMMUNICATION_SIGNALS):
                if "communication" not in agent_domains:
                    agent_domains.append("communication")
            if any(kw in q_lower for kw in _GENERAL_SIGNALS):
                if "general" not in agent_domains:
                    agent_domains.append("general")

        return agent_domains

    async def _retrieve_domain(
        self,
        domain: str,
        query: str,
        suite_id: str | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Retrieve chunks from a single domain. Returns (domain, chunks)."""
        try:
            if domain == "legal":
                from aspire_orchestrator.services.legal_retrieval_service import (
                    get_retrieval_service,
                )
                svc = get_retrieval_service()
                result = await svc.retrieve(query, suite_id=suite_id)
                return (domain, result.chunks)

            elif domain == "finance":
                from aspire_orchestrator.services.financial_retrieval_service import (
                    get_financial_retrieval_service,
                )
                svc = get_financial_retrieval_service()
                result = await svc.retrieve(query, suite_id=suite_id)
                return (domain, result.chunks)

            elif domain == "general":
                from aspire_orchestrator.services.general_retrieval_service import (
                    get_general_retrieval_service,
                )
                svc = get_general_retrieval_service()
                result = await svc.retrieve(query, suite_id=suite_id)
                return (domain, result.chunks)

            elif domain == "communication":
                from aspire_orchestrator.services.communication_retrieval_service import (
                    get_communication_retrieval_service,
                )
                svc = get_communication_retrieval_service()
                result = await svc.retrieve(query, suite_id=suite_id)
                return (domain, result.chunks)

            else:
                logger.warning("Unknown domain for retrieval: %s", domain)
                return (domain, [])

        except Exception as e:
            logger.warning(
                "Domain %s retrieval failed (non-fatal): %s", domain, e,
            )
            return (domain, [])

    def _assemble_context(
        self,
        domain_results: list[tuple[str, list[dict[str, Any]]]],
    ) -> str:
        """Assemble retrieved chunks into unified context string."""
        all_chunks: list[tuple[str, dict[str, Any]]] = []
        for domain, chunks in domain_results:
            for chunk in chunks:
                all_chunks.append((domain, chunk))

        if not all_chunks:
            return ""

        # Sort by combined_score descending across all domains
        all_chunks.sort(
            key=lambda x: x[1].get("combined_score", 0) or x[1].get("vector_similarity", 0),
            reverse=True,
        )

        # Take top 10 across all domains
        top_chunks = all_chunks[:10]

        lines = ["--- RELEVANT KNOWLEDGE ---"]
        total = len(top_chunks)

        for i, (domain, chunk) in enumerate(top_chunks, 1):
            similarity = chunk.get("combined_score") or chunk.get("vector_similarity", 0)
            chunk_type = chunk.get("chunk_type", "")
            content = chunk.get("content", "")

            meta_parts = [f"Source: {domain}"]
            if chunk_type:
                meta_parts.append(f"Type: {chunk_type}")
            meta_parts.append(f"Relevance: {similarity:.2f}")

            lines.append(f"\n[{i}/{total}] {' | '.join(meta_parts)}")
            lines.append(content.strip())

        lines.append("\n--- END KNOWLEDGE ---")
        return "\n".join(lines)

    def _make_retrieval_receipt(
        self,
        *,
        receipt_id: str,
        suite_id: str | None,
        agent_id: str,
        domains_queried: list[str],
        total_chunks: int,
        timing_ms: float,
    ) -> dict[str, Any]:
        """Generate receipt for cross-domain retrieval (Law #2)."""
        receipt = {
            "receipt_id": receipt_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": "rag.cross_domain_retrieval",
            "actor": "service:retrieval-router",
            "suite_id": suite_id or "system",
            "action_type": "rag.route_and_retrieve",
            "risk_tier": "green",
            "tool_used": "retrieval_router",
            "outcome": "success",
            "reason_code": "EXECUTED",
            "agent_id": agent_id,
            "domains_queried": domains_queried,
            "total_chunks": total_chunks,
            "timing_ms": round(timing_ms, 2),
        }
        store_receipts([receipt])
        return receipt

    async def retrieve(
        self,
        query: str,
        agent_id: str,
        suite_id: str | None = None,
    ) -> RetrievalRouterResult:
        """Route query to relevant domain RAGs and retrieve in parallel.

        Args:
            query: User utterance / search query
            agent_id: Target agent (determines primary domain)
            suite_id: Tenant ID for scoped search

        Returns:
            RetrievalRouterResult with assembled context and metadata.
            Returns empty context on any failure (graceful degradation).
        """
        start = time.monotonic()
        receipt_id = f"rcpt-rr-{uuid.uuid4().hex[:12]}"

        if not query or not query.strip():
            return RetrievalRouterResult(receipt_id=receipt_id)

        # 1. Determine which domains to query
        domains = self._determine_domains(agent_id, query)

        if not domains:
            logger.debug(
                "No RAG domains for agent=%s, skipping retrieval", agent_id,
            )
            return RetrievalRouterResult(
                receipt_id=receipt_id,
                timing_ms=(time.monotonic() - start) * 1000,
            )

        # 2. Parallel retrieval across all needed domains
        try:
            domain_results = await asyncio.gather(*[
                self._retrieve_domain(domain, query, suite_id)
                for domain in domains
            ])
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "Cross-domain retrieval failed (non-fatal): %s (%.1fms)",
                e, elapsed,
            )
            return RetrievalRouterResult(
                receipt_id=receipt_id,
                timing_ms=elapsed,
            )

        # 3. Assemble unified context
        context = self._assemble_context(list(domain_results))
        total_chunks = sum(len(chunks) for _, chunks in domain_results)

        elapsed = (time.monotonic() - start) * 1000

        # 4. Receipt (Law #2)
        receipt = self._make_retrieval_receipt(
            receipt_id=receipt_id,
            suite_id=suite_id,
            agent_id=agent_id,
            domains_queried=domains,
            total_chunks=total_chunks,
            timing_ms=elapsed,
        )

        logger.info(
            "RetrievalRouter: agent=%s domains=%s chunks=%d in %.1fms",
            agent_id, domains, total_chunks, elapsed,
        )

        return RetrievalRouterResult(
            context=context,
            domains_queried=domains,
            total_chunks=total_chunks,
            timing_ms=elapsed,
            receipt_id=receipt_id,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_router: RetrievalRouter | None = None


def get_retrieval_router() -> RetrievalRouter:
    """Get or create the singleton retrieval router."""
    global _router
    if _router is None:
        _router = RetrievalRouter()
    return _router
