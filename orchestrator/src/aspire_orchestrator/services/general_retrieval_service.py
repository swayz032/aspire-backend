"""General Retrieval Service — Hybrid search pipeline for Ava RAG.

Ava's general knowledge base covering:
  - aspire_platform: How Aspire works, features, agents, governance
  - business_operations: General business management, planning, strategy
  - industry_knowledge: Industry-specific insights for SMB verticals
  - best_practices: Operational best practices, productivity, efficiency

Built on BaseRetrievalService. Graceful degradation on failure.

Law compliance:
  - Law #2: Receipt for every retrieval operation
  - Law #3: Fail-closed on errors (returns empty, not guesses)
  - Law #6: Suite-scoped search (global + tenant knowledge)
"""

from __future__ import annotations

from aspire_orchestrator.services.base_retrieval_service import (
    BaseRetrievalService,
)


class GeneralRetrievalService(BaseRetrievalService):
    """Ava's general knowledge retrieval service.

    Searches the general_knowledge_chunks table via the
    search_general_knowledge Supabase RPC function.
    """

    search_function = "search_general_knowledge"
    search_table = "general_knowledge_chunks"
    actor_name = "service:ava-rag-retrieval"
    cache_prefix = "general_rag"
    domain_label = "GENERAL KNOWLEDGE (Ava RAG)"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: GeneralRetrievalService | None = None


def get_general_retrieval_service() -> GeneralRetrievalService:
    """Get or create the singleton general retrieval service."""
    global _service
    if _service is None:
        _service = GeneralRetrievalService()
    return _service
