"""VisionBudget — Wave 19.2 / Commit 4 cost meter for cascade vision methods.

One ``VisionBudget`` instance per cascade-batch run (constructed by
``resolve_batch``) is shared across every candidate's resolver methods.
Vision methods call ``budget.can_spend()`` before issuing the OpenAI call,
record token usage via ``record_call()`` afterward, and abort cleanly
when the per-batch USD ceiling is hit.

Pricing (gpt-5 family, verified against OpenAI public pricing page, 2026-05):
  * gpt-5.4-mini  — $0.000150 / 1K input,  $0.000600 / 1K output
  * gpt-5         — $0.002500 / 1K input,  $0.010000 / 1K output
  * gpt-5.2       — $0.003750 / 1K input,  $0.015000 / 1K output
  * gpt-5.4       — $0.000150 / 1K input,  $0.000600 / 1K output  (alias for mini)

If a model name is unrecognised we use the gpt-5 rate as a SAFE-HIGH
fallback (defensive overestimate — would rather abort early than overrun).

Cost ceiling
------------
* Default: 0.30 USD per batch (set via ``ASPIRE_DREW_VISION_CAP_USD`` env).
* When the ceiling would be exceeded by the next call, ``can_spend()`` returns
  False, vision method emits ``skip_reason="vision_budget_exhausted"``,
  and other (OCR / geometric) methods in the same cascade still run.
* Pre-spend reservation: if a budget call comes in with no token estimate
  yet (the first call of the batch), we admit it unconditionally. After it
  returns we record actual usage; the NEXT call's ``can_spend`` reflects
  the real spend.

Law compliance
--------------
* Law #1 — VisionBudget reports state; resolvers decide whether to skip.
* Law #2 — Spend totals are surfaced on cascade receipts via the
  evidence allow-list (vision_tokens_used, vision_cost_usd_estimate,
  vision_calls_aborted_by_budget).
* Law #3 — Conservative pricing fallback on unknown models prevents
  silent overrun.
* Law #9 — No prompt content, no model output stored on the budget;
  only token counts and USD estimate.

Thread safety
-------------
Single-event-loop, single-task usage. The cascade dispatches via
``asyncio.gather`` which serialises around the event loop; concurrent
``record_call`` writes never race because Python attribute writes are
atomic under the GIL. Do NOT share a VisionBudget across event loops.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)


# Per-1K-token USD rates. Conservative for unknown models.
_MODEL_RATES_USD_PER_1K: dict[str, tuple[float, float]] = {
    # (input_per_1k, output_per_1k)
    "gpt-5.4-mini": (0.000150, 0.000600),
    "gpt-5.4": (0.000150, 0.000600),
    "gpt-5-mini": (0.000150, 0.000600),
    "gpt-5": (0.002500, 0.010000),
    "gpt-5.2": (0.003750, 0.015000),
}
_FALLBACK_RATE_USD_PER_1K: tuple[float, float] = (0.002500, 0.010000)  # gpt-5 tier

_DEFAULT_CAP_USD: float = 0.30
_ENV_CAP_VAR: str = "ASPIRE_DREW_VISION_CAP_USD"


def _resolve_cap_usd() -> float:
    """Read the per-batch USD ceiling from env, falling back to default.

    Negative or zero values are rejected (would disable the meter
    entirely — fail closed). Non-numeric values fall back to default
    with a warning.
    """
    raw = os.getenv(_ENV_CAP_VAR, "").strip()
    if not raw:
        return _DEFAULT_CAP_USD
    try:
        val = float(raw)
    except ValueError:
        _log.warning(
            "vision_budget: %s=%r is not a number; using default %.2f USD",
            _ENV_CAP_VAR, raw, _DEFAULT_CAP_USD,
        )
        return _DEFAULT_CAP_USD
    if val <= 0:
        _log.warning(
            "vision_budget: %s=%.4f is non-positive; using default %.2f USD",
            _ENV_CAP_VAR, val, _DEFAULT_CAP_USD,
        )
        return _DEFAULT_CAP_USD
    return val


def estimate_cost_usd(
    *, model: str, input_tokens: int, output_tokens: int
) -> float:
    """Compute USD cost for one OpenAI call. Pure function — easy to unit-test."""
    rates = _MODEL_RATES_USD_PER_1K.get(model, _FALLBACK_RATE_USD_PER_1K)
    in_rate, out_rate = rates
    return (input_tokens / 1000.0) * in_rate + (output_tokens / 1000.0) * out_rate


@dataclass
class VisionBudget:
    """Per-cascade-batch USD ceiling on vision API spend.

    Attached to every ``MissingInputCandidate`` via the same path that
    ``data_provider`` flows through (set in ``resolve_batch``). Vision
    methods consult ``can_spend()`` before issuing a call and
    ``record_call()`` after to update running spend.
    """
    cap_usd: float = field(default_factory=_resolve_cap_usd)
    tokens_input_used: int = 0
    tokens_output_used: int = 0
    cost_usd_spent: float = 0.0
    calls_made: int = 0
    calls_aborted_by_budget: int = 0

    def can_spend(self) -> bool:
        """Return True if we still have headroom for another vision call.

        Conservative semantics: returns True only while spend < cap.
        We do NOT pre-reserve an expected-spend budget for the next call
        because token counts are unknown until response returns — the
        first call always admits, subsequent calls reflect actual spend.
        """
        return self.cost_usd_spent < self.cap_usd

    def record_call(
        self, *, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Record one successful vision call. Returns the call's USD cost.

        Safe to call with zero-token usage (e.g. if the OpenAI response
        carried no usage block — defensive fallback).
        """
        cost = estimate_cost_usd(
            model=model, input_tokens=input_tokens, output_tokens=output_tokens,
        )
        self.tokens_input_used += max(0, int(input_tokens or 0))
        self.tokens_output_used += max(0, int(output_tokens or 0))
        self.cost_usd_spent += cost
        self.calls_made += 1
        return cost

    def record_abort(self) -> None:
        """Record that a vision call was skipped because the budget was exhausted."""
        self.calls_aborted_by_budget += 1

    def receipt_evidence(self) -> dict[str, int | float]:
        """Return a Law-9-safe summary for inclusion in resolver evidence.

        Keys MUST appear in
        ``missing_input_cascade._RECEIPT_EVIDENCE_KEY_ALLOWLIST`` to be
        persisted on the cascade receipt. See Wave 19.2 entry in the
        allow-list.
        """
        return {
            "vision_tokens_used": self.tokens_input_used + self.tokens_output_used,
            "vision_input_tokens": self.tokens_input_used,
            "vision_output_tokens": self.tokens_output_used,
            "vision_cost_usd_estimate": round(self.cost_usd_spent, 5),
            "vision_calls_made": self.calls_made,
            "vision_calls_aborted_by_budget": self.calls_aborted_by_budget,
            "vision_cap_usd": round(self.cap_usd, 4),
        }


__all__ = ["VisionBudget", "estimate_cost_usd"]
