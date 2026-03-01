"""Communication Retrieval Service — Hybrid search pipeline for Eli RAG.

Eli's communication knowledge base covering:
  - email_best_practices: Subject lines, timing, structure, formatting
  - client_communication: Follow-up cadence, escalation, relationship management
  - business_writing: Professional tone, templates, drafting patterns
  - tone_guidance: Formality calibration, industry-appropriate language

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


class CommunicationRetrievalService(BaseRetrievalService):
    """Eli's communication knowledge retrieval service.

    Searches the communication_knowledge_chunks table via the
    search_communication_knowledge Supabase RPC function.
    """

    search_function = "search_communication_knowledge"
    actor_name = "service:eli-rag-retrieval"
    cache_prefix = "comm_rag"
    domain_label = "COMMUNICATION KNOWLEDGE (Eli RAG)"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: CommunicationRetrievalService | None = None


def get_communication_retrieval_service() -> CommunicationRetrievalService:
    """Get or create the singleton communication retrieval service."""
    global _service
    if _service is None:
        _service = CommunicationRetrievalService()
    return _service
