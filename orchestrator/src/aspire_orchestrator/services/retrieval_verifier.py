"""Bounded grounding verifier for retrieval-backed conversational responses."""

from __future__ import annotations

from dataclasses import dataclass, field

from aspire_orchestrator.config.settings import settings


_KNOWLEDGE_INTENTS = {"knowledge", "advice", "conversation", "question"}


@dataclass(frozen=True)
class RetrievalVerificationReport:
    passed: bool
    mode: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    fallback_text: str = ""


def _default_fallback(agent_id: str) -> str:
    fallback_map = {
        "ava": "I can help with that, but I don't have enough grounded context yet. Give me the exact detail you want checked and I'll keep it precise.",
        "finn": "I can work through it, but I don't have enough grounded finance context yet. Give me the exact number or scenario you want checked.",
        "eli": "I can help, but I don't have enough grounded communication context yet. Tell me the exact message or thread you want handled.",
        "nora": "I can help, but I don't have enough grounded scheduling context yet. Tell me the exact meeting detail you want checked.",
        "clara": "I can help, but I don't have enough grounded legal context yet. Tell me the clause or contract point you want reviewed.",
    }
    return fallback_map.get(
        agent_id,
        "I can help, but I don't have enough grounded context yet. Tell me the exact point you want checked.",
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
