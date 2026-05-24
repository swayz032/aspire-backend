"""Pass 1.5a regression lock — per-stage timeout ceilings.

Wave 27 validation on 2026-05-24 (project 7a93d144) tripped the SEE-stage 300s
TimeoutError on a 39-sheet Gulfstream re-ingest because Modal GPU is disabled
and local-CPU Ultralytics needs ~7-8 min. We bumped SEE to 900s (15 min). The
other stages were left untouched because they remain well within their ceilings.

This test locks the values so a future "make it faster" refactor can't silently
shrink the SEE budget below the verified local-CPU runtime.
"""
from __future__ import annotations

from aspire_orchestrator.routes.blueprint_dispatch import _STAGE_TIMEOUTS


def test_see_timeout_is_900s() -> None:
    """SEE must hold 900s (15 min) until Modal GPU lands (Wave 20.6)."""
    assert _STAGE_TIMEOUTS["see"] == 15 * 60, (
        "SEE timeout must be 900s — local-CPU Ultralytics on 39 sheets needs ~7-8 min "
        "(Wave 27 validation, 2026-05-24). Do not shrink without Modal in place."
    )


def test_other_stage_timeouts_unchanged_by_pass_1_5a() -> None:
    """Pass 1.5a touched ONLY the SEE timeout; lock the rest."""
    assert _STAGE_TIMEOUTS["ingest"] == 8 * 60
    assert _STAGE_TIMEOUTS["classify"] == 3 * 60
    assert _STAGE_TIMEOUTS["reason"] == 5 * 60
    assert _STAGE_TIMEOUTS["procure"] == 2 * 60
