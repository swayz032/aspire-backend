"""Telephony Policy — Sarah (Front Desk) runtime policy enforcement.

Pure policy module for telephony call governance. No provider calls.
Enforces:
  - Forbidden topic detection (PII/financial data protection, Law #9)
  - Handle time limits (3 minutes max per call)
  - Escalation thresholds (escalate after 2 minutes)

This is used by the orchestrator to validate telephony intent parameters
before routing to Twilio call execution.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class TelephonyPolicy:
    """Sarah Front Desk telephony policy enforcement.

    Governs call safety, handle time limits, and escalation triggers.
    """

    # Maximum call handle time (seconds) — 3 minutes
    MAX_HANDLE_TIME_S: float = 180.0

    # Escalation threshold (seconds) — escalate after 2 minutes
    ESCALATION_THRESHOLD_S: float = 120.0

    # Topics that Sarah must NOT discuss or engage with
    # These require human escalation per Aspire governance
    FORBIDDEN_TOPICS: list[str] = [
        "billing",
        "credit card",
        "bank account",
        "payment",
        "social security",
        "password",
    ]

    # Precompiled regex pattern for efficient matching
    _FORBIDDEN_PATTERN: re.Pattern[str] = re.compile(
        "|".join(re.escape(topic) for topic in FORBIDDEN_TOPICS),
        re.IGNORECASE,
    )

    @classmethod
    def check_topic_safety(cls, text: str) -> bool:
        """Check if text is safe from forbidden topics.

        Args:
            text: Text to check (e.g., call script, TwiML prompt, user request)

        Returns:
            True if text is SAFE (no forbidden topics found).
            False if text contains forbidden topics (must deny/escalate).
        """
        if not text:
            return True  # Empty text is safe

        match = cls._FORBIDDEN_PATTERN.search(text)
        if match:
            logger.warning(
                "Telephony topic safety violation: forbidden topic detected "
                "(matched=%r, position=%d-%d)",
                match.group(),
                match.start(),
                match.end(),
            )
            return False

        return True

    @classmethod
    def should_escalate(cls, elapsed_seconds: float) -> bool:
        """Check if a call should be escalated to a human.

        Args:
            elapsed_seconds: Time elapsed since call started.

        Returns:
            True if call has exceeded escalation threshold
            and should be handed off to a human agent.
        """
        return elapsed_seconds >= cls.ESCALATION_THRESHOLD_S

    @classmethod
    def is_within_handle_time(cls, elapsed_seconds: float) -> bool:
        """Check if call is within acceptable handle time.

        Args:
            elapsed_seconds: Time elapsed since call started.

        Returns:
            True if call is within max handle time.
            False if call has exceeded max handle time and must be ended.
        """
        return elapsed_seconds < cls.MAX_HANDLE_TIME_S

    @classmethod
    def get_forbidden_topics(cls) -> list[str]:
        """Return the list of forbidden topics for transparency/auditing."""
        return list(cls.FORBIDDEN_TOPICS)
