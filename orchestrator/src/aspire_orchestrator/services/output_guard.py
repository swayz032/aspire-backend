"""Output Guard — Phantom execution claim removal.

Ported from output_guard.ts (152 lines). Strips first-person execution claims
from LLM output when no receipt confirms execution. Enforces consultant plan
scaffold for user surface.
"""

from __future__ import annotations

import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

EXECUTION_CLAIM_RE = re.compile(
    r"\b(?:i|we)\s+(?:sent|emailed|texted|called|booked|scheduled|paid|charged|"
    r"transferred|moved|wired|submitted|signed|executed|completed|processed|"
    r"created|updated|deleted|purchased|filed|deposited|withdrew|invoiced|billed)\b",
    re.IGNORECASE,
)

EXECUTION_VERB_RE = re.compile(
    r"\b(?:sent|emailed|texted|called|booked|scheduled|paid|charged|transferred|"
    r"moved|wired|submitted|signed|executed|completed|processed|"
    r"created|updated|deleted|purchased|filed|deposited|withdrew|invoiced|billed)\b",
    re.IGNORECASE,
)


def _has_receipts(receipts: list[dict[str, Any]] | None) -> bool:
    if not receipts:
        return False
    return any(r.get("outcome") == "success" for r in receipts)


def _strip_execution_claims(text: str) -> tuple[str, bool]:
    """Split on sentence boundaries, remove sentences with execution claims."""
    if not text:
        return text, False

    parts = re.split(r"(?<=[.!?])\s+", text)
    changed = False
    kept = []
    for s in parts:
        s = s.strip()
        if not s:
            continue
        if EXECUTION_CLAIM_RE.search(s):
            changed = True
            continue
        kept.append(s)
    return " ".join(kept), changed


_PLAN_SCAFFOLD_SECTIONS = ["snapshot:", "nba:", "delegate:", "checkpoint:"]


def _ensure_plan_scaffold(text: str, skillpack_id: str = "ava") -> tuple[str, bool]:
    """Enforce consultant plan scaffold for user surface.

    Ported from v1.5 output_guard.ts ensureUserPlanSections().
    Checks for Snapshot/NBA/Delegate/Checkpoint sections.
    If missing, appends a deterministic consultant-grade scaffold.
    """
    lower_text = text.lower()
    has_all = all(section in lower_text for section in _PLAN_SCAFFOLD_SECTIONS)
    if has_all:
        return text, False

    scaffold = (
        "\n\n---\n"
        "**Snapshot:** Review current signals (inbox, calendar, open loops) and today's constraints.\n"
        "**Constraint:** Identify the single bottleneck driving today's urgency.\n"
        f"**NBA:** Produce one approval-ready artifact via {skillpack_id} (draft/proposal only).\n"
        f"**Delegate:** Route drafting work to {skillpack_id}. Approve before any external action.\n"
        "**Checkpoint:** Confirm progress via receipts or user confirmation within 24 hours."
    )
    return text + scaffold, True


def _check_document_quality(
    text: str,
    receipts: list[dict[str, Any]] | None,
) -> tuple[str, bool]:
    """Check if a Clara document creation has quality issues.

    When Clara creates a document with missing tokens (needs_additional_info=True),
    add a quality note so the user knows the document needs more data.
    """
    if not receipts:
        return text, False

    for r in receipts:
        tool_result = r.get("tool_result") or r.get("data") or {}
        if not isinstance(tool_result, dict):
            continue

        # Check for needs_info from preflight gate
        if tool_result.get("needs_info"):
            msg = tool_result.get("message_for_ava", "")
            if msg and msg not in text:
                return f"{text}\n{msg}", True
            return text, False

        # Check for post-creation quality warnings
        if tool_result.get("needs_additional_info"):
            quality = tool_result.get("token_quality", {})
            fill_rate = quality.get("fill_rate_pct", 100)
            missing = quality.get("missing_tokens", [])
            if fill_rate < 70 and missing:
                note = (
                    f"Note: This document has {len(missing)} blank field(s) "
                    f"({fill_rate:.0f}% filled). Review before sending for signature."
                )
                if note not in text:
                    return f"{text}\n{note}", True

    return text, False


def guard_output(
    text: str,
    receipts: list[dict[str, Any]] | None,
    outcome: str,
    surface: str = "user",
    skillpack_id: str = "ava",
    tool_results: list[dict[str, Any]] | None = None,
    channel: str = "chat",
) -> str:
    """Strip phantom execution claims and enforce consultant plan scaffold.

    Args:
        text: LLM-generated or template response text
        receipts: Pipeline receipts (tool_receipts)
        outcome: Pipeline outcome (success/pending/failed/denied)
        surface: "user" or "admin"
        skillpack_id: Agent routing target (for plan scaffold delegation step)
        tool_results: Optional tool execution results for quality checks
        channel: Interaction channel — "voice", "chat", "video". Scaffold is
                 skipped for voice/chat/video to keep responses natural for TTS.

    Returns:
        Guarded text with execution claims removed if no receipts confirm them.
    """
    if not text:
        return text

    added_codes: list[str] = []

    # Document quality check — warn about incomplete Clara documents
    quality_source = tool_results or receipts
    quality_text, quality_changed = _check_document_quality(text, quality_source)
    if quality_changed:
        text = quality_text
        added_codes.append("document_quality_warning")

    # Remove execution claims when no success receipts exist
    if not _has_receipts(receipts):
        cleaned, changed = _strip_execution_claims(text)
        if changed:
            text = cleaned
            added_codes.append("execution_claim_sanitized")
            disclaimer = (
                "Note: This is a draft proposal. Review it in your Authority Queue "
                "for accuracy — nothing has been sent or charged."
            )
            text = f"{text}\n{disclaimer}" if text.strip() else disclaimer

    # Pending outcome: add draft disclaimer
    if outcome in ("pending", "approval_required") and "draft proposal" not in text.lower():
        if "Nothing has been sent" not in text:
            text = (
                f"{text}\nNote: Review the details in your Authority Queue for accuracy "
                "before approving. Nothing is sent or charged until you approve."
            )

    # User surface: enforce consultant plan scaffold (v1.5 ensureUserPlanSections)
    # Skip scaffold for conversational channels — it injects markdown that TTS reads
    # literally and makes chat responses sound robotic
    if (
        surface == "user"
        and outcome in ("pending", "approval_required", "success")
        and channel not in ("voice", "chat", "video", "text")
    ):
        scaffolded, scaffold_changed = _ensure_plan_scaffold(text, skillpack_id)
        if scaffold_changed:
            text = scaffolded
            added_codes.append("plan_scaffold_applied")

    # Soft guard: execution verbs in text without receipts
    if EXECUTION_VERB_RE.search(text) and not _has_receipts(receipts):
        reminder = "Reminder: Any send/charge/booking requires explicit approval and execution receipts."
        if reminder not in text:
            text = f"{text}\n{reminder}"
            added_codes.append("execution_reminder_added")

    if added_codes:
        logger.info("Output guard applied: %s", ", ".join(added_codes))

    return text
