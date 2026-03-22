"""Bounded grounding verifier for retrieval-backed conversational responses."""

from __future__ import annotations

from dataclasses import dataclass, field

from aspire_orchestrator.config.settings import settings


_KNOWLEDGE_INTENTS = {"knowledge", "advice", "question"}


@dataclass(frozen=True)
class RetrievalVerificationReport:
    passed: bool
    mode: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    fallback_text: str = ""


def _default_fallback(agent_id: str) -> str:
    fallback_map = {
        "ava": "Here is the best grounded answer I can give right now. Which exact detail should I verify first?",
        "finn": "I can give you a conservative read now. Which number or timeframe should I verify first?",
        "eli": "I can draft a safe first pass now. Which exact thread or message should I verify first?",
        "nora": "I can give you a safe scheduling direction now. Which meeting detail should I verify first?",
        "clara": "I can give a cautious legal read now. Which clause or contract section should I verify first?",
    }
    return fallback_map.get(
        agent_id,
        "I can give you a safe first pass now. Which exact point should I verify first?",
    )


def verify_retrieval_grounding(
    *,
    intent_type: str,
    retrieval_status: str,
    grounding_score: float,
    agent_id: str,
    conflict_flags: list[str] | None = None,
) -> RetrievalVerificationReport:
    """Decide whether a retrieval-backed response is grounded enough to answer directly."""
    normalized_intent = (intent_type or "").strip().lower()
    if normalized_intent not in _KNOWLEDGE_INTENTS:
        return RetrievalVerificationReport(
            passed=True,
            mode="not_applicable",
            confidence=1.0,
        )

    # Conversational responses with no retrieval data should pass through —
    # requiring grounding when there's nothing to ground against makes no sense.
    # Retrieval router returns "degraded" with score=0.0 when vector RPCs fail
    # or no chunks are found, "not_applicable"/"skipped" when RAG was bypassed.
    if grounding_score == 0.0 or retrieval_status in {"not_applicable", "skipped"}:
        return RetrievalVerificationReport(
            passed=True,
            mode="no_retrieval",
            confidence=1.0,
        )

    conflicts = [flag for flag in (conflict_flags or []) if flag]
    reasons: list[str] = []
    mode = "grounded"

    if retrieval_status in {"offline", "degraded"}:
        reasons.append(f"retrieval_status={retrieval_status}")
        mode = "degraded"

    if conflicts:
        reasons.extend(conflicts)
        mode = "conflict"

    if grounding_score < float(settings.retrieval_min_grounding_score):
        reasons.append(f"grounding_score_below_threshold:{grounding_score:.2f}")
        mode = "weak_grounding"

    if reasons:
        return RetrievalVerificationReport(
            passed=False,
            mode=mode,
            confidence=max(0.0, min(1.0, grounding_score)),
            reasons=reasons,
            fallback_text=_default_fallback(agent_id),
        )

    return RetrievalVerificationReport(
        passed=True,
        mode=mode,
        confidence=max(0.0, min(1.0, grounding_score)),
    )
