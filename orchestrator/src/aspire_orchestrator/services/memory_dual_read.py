"""Dual-read shadow mode for the Office Memory Engine migration (Pass 7).

During the cutover window between the legacy per-agent memory tables
(`agent_episodes`, `agent_semantic_memory`) and the new `memory_objects` spine,
we run BOTH read paths in parallel and log any divergence at WARNING level.
The legacy result is ALWAYS the one returned to the caller -- the new path is
shadow-only. Production traffic is never blocked by failures in the new path.

Why this exists:
  1. Verifies parity in production traffic before Pass 12 cutover.
  2. Catches RLS / scope / index mismatches that only surface at scale.
  3. Lets us measure new-path latency under real load before flipping.

Aspire Laws enforced:
  Law #2 (Receipts)    -- shadow reads do not emit receipts (read-only).
  Law #3 (Fail Closed) -- new-path exceptions are caught and logged; the
                          legacy path is unaffected.
  Law #6 (Tenant Iso)  -- new-path queries are scope-bound via the same
                          (tenant_id, suite_id, office_id) the caller used.
  Law #9 (Security)    -- log lines never contain PII (summary text, fact_value);
                          we log only counts, id sets, and divergence shape.

Toggle: ASPIRE_MEMORY_DUAL_READ_ENABLED=1|0 (default 1).
        Set to 0 if the new path causes production incidents.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def is_dual_read_enabled() -> bool:
    """Return True iff dual-read shadow mode is on for this process.

    Lazy-imports settings so this module is safe to import at orchestrator
    startup before the Pydantic settings cache is primed.
    """
    try:
        from aspire_orchestrator.config.settings import settings
        return bool(getattr(settings, "memory_dual_read_enabled", True))
    except Exception:
        # Default to enabled (Law #3-style: fail visible, not silent).
        return True


def log_divergence(
    *,
    surface: str,
    legacy_ids: Iterable[str],
    shadow_ids: Iterable[str],
    legacy_count: int,
    shadow_count: int,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a structured WARNING when legacy and shadow result sets disagree.

    Args:
        surface: Identifier for the call site, e.g. "episodic_memory.search".
        legacy_ids: IDs returned by the legacy table read.
        shadow_ids: IDs returned by the shadow memory_objects read.
        legacy_count: Total count from legacy path (may differ from len(ids) if pagination).
        shadow_count: Total count from shadow path.
        extra: Optional structured fields to append (no PII allowed).

    Behaviour:
      - If sets match exactly, emits a DEBUG log (so we can verify parity without warning fatigue).
      - If sets diverge, emits a WARNING log with the symmetric diff sizes.
      - Never raises -- divergence reporting is best-effort.
    """
    try:
        legacy_set = {str(x) for x in legacy_ids if x is not None}
        shadow_set = {str(x) for x in shadow_ids if x is not None}

        only_legacy = legacy_set - shadow_set
        only_shadow = shadow_set - legacy_set

        if not only_legacy and not only_shadow and legacy_count == shadow_count:
            logger.debug(
                "memory_dual_read parity ok: surface=%s count=%d",
                surface, legacy_count,
            )
            return

        payload: dict[str, Any] = {
            "surface": surface,
            "legacy_count": legacy_count,
            "shadow_count": shadow_count,
            "only_legacy_count": len(only_legacy),
            "only_shadow_count": len(only_shadow),
        }
        if extra:
            payload.update({k: v for k, v in extra.items() if not _looks_like_pii(k)})

        logger.warning(
            "memory_dual_read DIVERGENCE: surface=%s legacy=%d shadow=%d "
            "only_legacy=%d only_shadow=%d %s",
            surface, legacy_count, shadow_count,
            len(only_legacy), len(only_shadow),
            payload,
        )
    except Exception as exc:  # pragma: no cover - reporter must never raise
        logger.debug("memory_dual_read log_divergence failed: %s", exc)


def log_shadow_error(*, surface: str, error: BaseException) -> None:
    """Record a shadow-path failure without disrupting the legacy result."""
    logger.warning(
        "memory_dual_read shadow_error: surface=%s error_type=%s error=%s",
        surface, type(error).__name__, str(error)[:200],
    )


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

# Field name fragments we never want to forward into log payloads, just in case
# a caller passes through something dubious. We explicitly ignore PII-leaning
# fields even if a caller mistakenly includes them in `extra`.
_PII_HINTS = ("summary", "fact_value", "content", "email", "phone", "ssn", "raw")


def _looks_like_pii(field_name: str) -> bool:
    lower = field_name.lower()
    return any(hint in lower for hint in _PII_HINTS)
