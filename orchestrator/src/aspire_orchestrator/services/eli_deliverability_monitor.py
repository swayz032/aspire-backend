"""Eli Deliverability Monitor — health classification for outbound email.

Maps provider/postmaster-style signals into actionable states used by policy:
  - healthy
  - warning
  - blocked
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aspire_orchestrator.services.eli_quality_guard import load_eli_autonomy_policy


@dataclass(frozen=True)
class DeliverabilityStatus:
    level: str
    spam_rate: float
    reasons: list[str]


def evaluate_deliverability(signals: dict[str, Any] | None) -> DeliverabilityStatus:
    """Evaluate deliverability health from normalized signal inputs.

    Expected optional fields in `signals`:
      - spam_rate: float (0..1)
      - dkim_aligned: bool
      - spf_or_dkim_pass: bool
      - tls_enabled: bool
    """
    policy = load_eli_autonomy_policy()
    dcfg = policy.get("deliverability", {}) if isinstance(policy, dict) else {}
    warn_threshold = float(dcfg.get("max_spam_rate_warning", 0.10))
    block_threshold = float(dcfg.get("max_spam_rate_block", 0.30))
    enforce_dkim = bool(dcfg.get("enforce_dkim_alignment", True))
    enforce_auth = bool(dcfg.get("enforce_spf_or_dkim", True))
    enforce_tls = bool(dcfg.get("enforce_tls", True))

    sig = signals or {}
    spam_rate = float(sig.get("spam_rate", 0.0) or 0.0)
    reasons: list[str] = []

    blocked = False
    warning = False

    if spam_rate >= block_threshold:
        blocked = True
        reasons.append(f"spam_rate >= {block_threshold:.2f}")
    elif spam_rate >= warn_threshold:
        warning = True
        reasons.append(f"spam_rate >= {warn_threshold:.2f}")

    if enforce_dkim and not bool(sig.get("dkim_aligned", True)):
        blocked = True
        reasons.append("dkim alignment failed")
    if enforce_auth and not bool(sig.get("spf_or_dkim_pass", True)):
        blocked = True
        reasons.append("spf/dkim auth failed")
    if enforce_tls and not bool(sig.get("tls_enabled", True)):
        warning = True
        reasons.append("tls disabled")

    if blocked:
        level = "blocked"
    elif warning:
        level = "warning"
    else:
        level = "healthy"

    return DeliverabilityStatus(level=level, spam_rate=spam_rate, reasons=reasons)
