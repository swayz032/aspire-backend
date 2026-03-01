"""Conference Retrieval Service — Hybrid search pipeline for Nora RAG.

Nora's conference knowledge base covering:
  - meeting_best_practices: Facilitation, time management, note-taking, action items
  - video_etiquette: Professional presence, background setup, camera/audio standards
  - collaboration: Agenda design, breakout strategies, participant engagement
  - virtual_tools: Platform features, sharing, recording, accessibility

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


class ConferenceRetrievalService(BaseRetrievalService):
    """Nora's conference knowledge retrieval service.

    Searches the conference_knowledge_chunks table via the
    search_conference_knowledge Supabase RPC function.
    """

    search_function = "search_conference_knowledge"
    actor_name = "service:nora-rag-retrieval"
    cache_prefix = "conference_rag"
    domain_label = "CONFERENCE KNOWLEDGE (Nora RAG)"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: ConferenceRetrievalService | None = None


def get_conference_retrieval_service() -> ConferenceRetrievalService:
    """Get or create the singleton conference retrieval service."""
    global _service
    if _service is None:
        _service = ConferenceRetrievalService()
    return _service
