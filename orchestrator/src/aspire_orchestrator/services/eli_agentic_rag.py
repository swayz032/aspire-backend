"""Eli Agentic RAG orchestration for email/office drafting flows.

Production goals:
  - Always attempt communication RAG for Eli draft/send flows
  - Keep service online under RAG failures via safe deterministic fallback
  - Run bounded critique loops for output quality (max 2 iterations)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import re
from typing import Any

from aspire_orchestrator.services.communication_retrieval_service import (
    get_communication_retrieval_service,
)
from aspire_orchestrator.services.eli_email_param_helpers import (
    body_text_to_html,
    signoff_from_sender,
)
from aspire_orchestrator.services.eli_quality_guard import evaluate_email_quality
from aspire_orchestrator.services.eli_quality_guard import load_eli_autonomy_policy

logger = logging.getLogger(__name__)

_SUPPORTED_TASKS = {"email.draft", "email.send"}
_DEFAULT_MAX_CRITIQUE_ITERS = 2


def _is_eli_email_task(task_type: str, assigned_agent: str) -> bool:
    return task_type in _SUPPORTED_TASKS and assigned_agent == "eli"


def _derive_query(task_type: str, utterance: str, params: dict[str, Any]) -> str:
    subject = str(params.get("subject", "")).strip()
    to_addr = str(params.get("to", "")).strip()
    base = utterance.strip() or f"{task_type} {subject}".strip()
    return f"{base} recipient:{to_addr} subject:{subject}".strip()


def _ensure_professional_shape(params: dict[str, Any]) -> dict[str, Any]:
    body = str(params.get("body_text", "")).strip()
    to_addr = str(params.get("to", "")).strip()
    from_addr = str(params.get("from_address", "")).strip() or None
    recipient_name = to_addr.split("@", 1)[0].replace(".", " ").title() if "@" in to_addr else "there"
    signoff = signoff_from_sender(from_addr)

    if body and not re.match(r"^(dear|hello|hi)\b", body, re.IGNORECASE):
        body = f"Hi {recipient_name},\n\n{body}"
    if body and not re.search(r"\b(please|let me know|can you|could you|confirm)\b", body, re.IGNORECASE):
        body = f"{body.rstrip()}\n\nPlease let me know what works best on your side."
    if body and not re.search(r"\n\n(best,|best regards,|regards,|thanks,|sincerely,|cheers,)", body, re.IGNORECASE):
        body = f"{body.rstrip()}\n\n{signoff}"

    if body:
        params["body_text"] = body
        params["body_html"] = body_text_to_html(body)
    return params


def _apply_rag_guidance(params: dict[str, Any], rag_context: str) -> dict[str, Any]:
    """Light deterministic guidance from retrieved communication context.

    We avoid free-form model generation here for reliability and latency.
    """
    body = str(params.get("body_text", "")).strip()
    if not body:
        return params

    lower_ctx = (rag_context or "").lower()

    # Purpose-first reinforcement from communication knowledge.
    if "open with purpose" in lower_ctx and re.search(r"hope (this email finds|you're well|you are well)", body, re.IGNORECASE):
        body = re.sub(
            r"(?i)hope (this email finds|you're|you are) well[,.!\s]*",
            "",
            body,
        ).strip()

    # Subject clarity reinforcement.
    subject = str(params.get("subject", "")).strip()
    if subject and len(subject) > 72:
        params["subject"] = subject[:72].rstrip(" -,:;")

    params["body_text"] = body
    params["body_html"] = body_text_to_html(body)
    return params


def _quality_mode_for_task(task_type: str) -> str:
    return "send" if task_type == "email.send" else "draft"


def _is_advanced_proposal_prompt(*, utterance: str, subject: str) -> bool:
    text = f"{utterance} {subject}".lower()
    signals = (
        "binding proposal",
        "roofing proposal",
        "proposal",
        "bid",
        "scope",
        "materials",
        "timeline",
        "permit",
        "warranty",
        "payment schedule",
    )
    return any(s in text for s in signals)


def _build_advanced_proposal_body(params: dict[str, Any], utterance: str) -> str:
    to_addr = str(params.get("to", "")).strip()
    subject = str(params.get("subject", "")).strip() or "Project Proposal"
    from_addr = str(params.get("from_address", "")).strip() or None
    recipient_name = to_addr.split("@", 1)[0].replace(".", " ").title() if "@" in to_addr else "Team"
    signoff = signoff_from_sender(from_addr)
    deadline = (datetime.now(UTC) + timedelta(days=7)).strftime("%A, %B %d, %Y")
    include_three_options = any(k in utterance.lower() for k in ("three pricing options", "3 pricing options", "price options"))
    include_permit = any(k in utterance.lower() for k in ("permit", "compliance"))
    include_warranty = "warranty" in utterance.lower()
    include_timeline = any(k in utterance.lower() for k in ("timeline", "mobilization", "start date"))
    include_materials = "materials" in utterance.lower()

    lines = [
        f"Hi {recipient_name},",
        "",
        f"Following up on your request, please find our binding proposal for {subject}.",
        "",
        "Proposed scope of work:",
        "- Site prep, protection, and controlled tear-off of existing roof system as required",
        "- Deck inspection and remediation where needed",
        "- Full installation, QA checkpoints, and final punch-closeout",
    ]

    if include_materials:
        lines += [
            "",
            "Materials and system:",
            "- Commercial-grade membrane system with compatible insulation package",
            "- Manufacturer-approved components across flashings, edges, and penetrations",
        ]

    if include_timeline:
        lines += [
            "",
            "Timeline and mobilization:",
            "- Mobilization within 7 business days of notice to proceed",
            "- Estimated field duration: 10-15 business days, weather dependent",
            "- Daily progress updates with milestone sign-offs",
        ]

    if include_permit:
        lines += [
            "",
            "Permits and compliance:",
            "- We coordinate permit submission and required inspections",
            "- Work is executed to local code and manufacturer specifications",
        ]

    if include_three_options:
        lines += [
            "",
            "Pricing options:",
            "- Option A (Base): code-compliant install and standard warranty package",
            "- Option B (Performance): upgraded thermal assembly and extended coverage",
            "- Option C (Premium): enhanced lifecycle system with highest durability profile",
        ]

    lines += [
        "",
        "Payment schedule:",
        "- 30% deposit at mobilization",
        "- 40% at material delivery and dry-in milestone",
        "- 30% at substantial completion and handoff",
    ]

    if include_warranty:
        lines += [
            "",
            "Warranty terms:",
            "- Manufacturer material warranty plus workmanship warranty from our team",
            "- Final warranty length aligns with the selected option and system approval",
        ]

    lines += [
        "",
        f"Acceptance: please confirm approval by {deadline}. Once approved, I will issue the final execution package and mobilization schedule the same day.",
        "",
        signoff,
    ]
    return "\n".join(lines).strip()


async def run_eli_agentic_rag(
    *,
    task_type: str,
    assigned_agent: str,
    utterance: str,
    suite_id: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run Eli agentic RAG on extracted params, with resilient fallback.

    Returns:
      updated_params, metadata
    """
    if not _is_eli_email_task(task_type, assigned_agent):
        return params, {
            "eli_rag_status": "not_applicable",
            "eli_fallback_mode": False,
            "eli_rag_sources": [],
            "eli_iteration_count": 0,
            "eli_agentic_plan": {},
            "eli_quality_report": {},
        }

    mode = _quality_mode_for_task(task_type)
    policy = load_eli_autonomy_policy()
    autonomy = policy.get("autonomy", {}) if isinstance(policy, dict) else {}
    max_iters = int(autonomy.get("max_agentic_iterations", _DEFAULT_MAX_CRITIQUE_ITERS))
    if max_iters < 1:
        max_iters = 1

    working = dict(params or {})
    plan = {
        "intent": task_type,
        "audience": "external_recipient",
        "tone": "professional",
        "cta_required": True,
        "max_iterations": max_iters,
    }

    rag_status = "primary"
    fallback_mode = False
    rag_sources: list[str] = []
    retrieval_chunks = 0

    query = _derive_query(task_type, utterance, working)
    try:
        svc = get_communication_retrieval_service()
        rag_result = await svc.retrieve(query, suite_id=suite_id)
        retrieval_chunks = len(rag_result.chunks)
        if retrieval_chunks > 0:
            rag_context = svc.assemble_rag_context(rag_result)
            working = _apply_rag_guidance(working, rag_context)
            rag_sources = list({
                str(c.get("domain", "")).strip()
                for c in rag_result.chunks
                if str(c.get("domain", "")).strip()
            })
        else:
            rag_status = "degraded"
            fallback_mode = True
    except Exception as e:
        logger.warning("Eli agentic RAG retrieval failed (non-fatal): %s", e)
        rag_status = "offline"
        fallback_mode = True

    # Always enforce a professional deterministic base shape.
    if _is_advanced_proposal_prompt(
        utterance=utterance,
        subject=str(working.get("subject", "")),
    ):
        working["body_text"] = _build_advanced_proposal_body(working, utterance)
        working["body_html"] = body_text_to_html(working["body_text"])
    working = _ensure_professional_shape(working)

    # Bounded critique loop for reliability + quality floor.
    iterations = 0
    quality = evaluate_email_quality(payload=working, mode=mode)
    while not quality.passed and iterations < max_iters:
        iterations += 1
        working = _ensure_professional_shape(working)
        quality = evaluate_email_quality(payload=working, mode=mode)

    metadata = {
        "eli_rag_status": rag_status,
        "eli_fallback_mode": fallback_mode,
        "eli_rag_sources": rag_sources,
        "eli_iteration_count": iterations,
        "eli_agentic_plan": plan,
        "eli_quality_report": {
            "score": quality.score,
            "passed": quality.passed,
            "violations": quality.violations,
            "warnings": quality.warnings,
            "body_word_count": quality.body_word_count,
            "subject_length": quality.subject_length,
            "retrieved_chunk_count": retrieval_chunks,
        },
    }
    return working, metadata
