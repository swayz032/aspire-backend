"""General-vision resolver — Wave 19.2 / Commit 4.

Handles missing_inputs whose descriptions reference "read-the-drawing"
questions that the structured-category resolvers can't answer:
fire-protection sprinkler counts, life-safety device placement, civil-
utility routing, plumbing risers, siamese connections, etc.

Routing
-------
``missing_input_cascade._CATEGORY_PATTERNS`` includes a new
``GENERAL_VISION`` pattern matching ``\bfire\s+protection\b``,
``\bsprinkler\b``, ``\blife\s+safety\b``, ``\bcivil\b``, ``\butility\b``,
``\bplumbing\s+riser\b``, and ``\bsiamese\b``. This pattern is anchored
AFTER ``schedule`` in the route list so a "fire protection SCHEDULE"
description still hits the structured schedule resolver (which has
Azure-table extraction).

Method
------
Single method: ``_method_vision_unknown_fallback``. Picks the most-
referenced sheet from the missing_input description (parses "Sheet 39",
"FP-101", etc.), falls back to the last sheet by convention. Sends the
PNG to gpt-5.4-mini vision with the literal ``candidate.description``
as the prompt and demands structured JSON with extracted counts /
dimensions / labels.

Confidence
----------
* Concrete answer with model confidence >= 0.85 AND >= 1 extracted_fact
  → 0.85 (resolves).
* Concrete answer with model confidence 0.65-0.85 → 0.65 (consensus
  eligible but won't standalone-resolve).
* Vague / "I cannot answer" / no extracted facts → 0.0 (skip, leaves row
  open for the contractor).

Law compliance
--------------
* Law #1 — Returns a ResolverResult; cascade persists.
* Law #2 — Token usage threads onto cascade receipt via vision_budget.
* Law #3 — No false positives. ``extracted_facts_count == 0`` always
  skips even when the model returns confidence > 0.85 (the model can
  hallucinate high confidence without actually finding anything).
* Law #6 — Provider-scoped PNG reads; suite_id flows through candidate.
* Law #9 — No raw model answers logged; only counts and short
  ``vision_answer_summary`` (first 80 chars).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
    MissingInputResolver,
    ResolverResult,
)

_log = logging.getLogger(__name__)

# Parse "Sheet 39", "FP-101", "M-001", or bare "39" from the description
# to find the most-referenced sheet.
_SHEET_REF_PATTERN = re.compile(
    r"\b(?:sheet\s+)?([A-Z]{1,3}-?\d{1,4}[A-Za-z]?)\b",
    re.IGNORECASE,
)


class GeneralVisionResolver(MissingInputResolver):
    """Resolver for fire-protection / life-safety / civil-utility / etc."""

    category = MissingInputCategory.GENERAL_VISION

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        # Single method (we'd add geometric / OCR fallbacks here if a
        # specific category demanded it). The cascade's outer 90s cap and
        # vision_budget enforce all bounds.
        result = await self._method_vision_unknown_fallback(candidate)
        return [result]

    async def _method_vision_unknown_fallback(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Vision pass on the most-referenced sheet with the literal description prompt."""
        try:
            provider = candidate.data_provider
            budget = candidate.vision_budget

            # ---------- Pick target sheet ----------
            referenced_codes = [
                m.group(1).upper().replace("-", "")
                for m in _SHEET_REF_PATTERN.finditer(candidate.description or "")
            ]
            # Count occurrences across sheets — pick the most-mentioned.
            sheet_score: dict[str, int] = {}
            for sheet in candidate.sheets:
                code = str(sheet.get("sheet_code") or "").upper().replace("-", "")
                if not code:
                    continue
                for ref in referenced_codes:
                    if ref in code or code in ref:
                        sheet_score[str(sheet.get("id") or "")] = (
                            sheet_score.get(str(sheet.get("id") or ""), 0) + 1
                        )

            target_sheet: dict[str, Any] | None = None
            if sheet_score:
                top_id = max(sheet_score, key=lambda k: sheet_score[k])
                for sheet in candidate.sheets:
                    if str(sheet.get("id")) == top_id:
                        target_sheet = sheet
                        break
            if target_sheet is None and candidate.sheets:
                # Convention: last sheet by created_at (typically the
                # detail / fire-protection / utility sheet trailing the set).
                target_sheet = candidate.sheets[-1]

            if target_sheet is None or not target_sheet.get("id"):
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_unknown_fallback",
                    evidence={
                        "skip_reason": "no_target_sheet_available",
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            sheet_id = str(target_sheet["id"])
            png_bytes: bytes | None = None
            if provider is not None:
                try:
                    png_bytes = await provider.get_sheet_png(sheet_id)
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "general_vision_resolver: provider fetch failed sheet=%s err=%s",
                        sheet_id[:8], type(exc).__name__,
                    )
            if not png_bytes:
                legacy = target_sheet.get("image_bytes")
                if isinstance(legacy, (bytes, bytearray)) and legacy:
                    png_bytes = bytes(legacy)

            # ---------- Vision call ----------
            from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
                call_vision_with_budget,
            )
            system_prompt = (
                "You are reading a construction-drawing sheet to answer a "
                "specific contractor question. Return STRICT JSON only.\n"
                "Schema: {\n"
                '  "answer": "<concrete answer or empty string>",\n'
                '  "extracted_facts": [\n'
                '    {"fact_type": "<count|dimension|label|location>", '
                '"value": "<concrete value>"}\n'
                "  ],\n"
                '  "confidence": <0.0 to 1.0>,\n'
                '  "notes": "<one-line evidence summary>"\n'
                "}\n"
                "Rules:\n"
                "  * Only return facts you can READ directly from the sheet.\n"
                "  * If the sheet does not contain the answer, return "
                '"answer": "", "extracted_facts": [], "confidence": 0.0\n'
                "  * Do NOT estimate or extrapolate. No false answers."
            )
            user_prompt = (
                "The contractor's question is reproduced verbatim below. "
                "Answer it using ONLY what is visible on this sheet.\n\n"
                f"Question: {candidate.description[:500]}"
            )
            vc = await call_vision_with_budget(
                png_bytes=png_bytes,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                vision_budget=budget,
                max_output_tokens=600,
                timeout_seconds=60.0,
            )

            if not vc.ok:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_unknown_fallback",
                    evidence={
                        "skip_reason": vc.skip_reason,
                        "vision_model_used": vc.model,
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            # ---------- Interpret + no-false-positive gate ----------
            payload = vc.json_payload
            answer = str(payload.get("answer") or "").strip()
            facts = payload.get("extracted_facts") or []
            model_conf = float(payload.get("confidence") or 0.0)
            notes = str(payload.get("notes") or "")[:80]

            fact_count = len(facts) if isinstance(facts, list) else 0

            # Hard gate: extracted_facts_count must be >= 1 AND model
            # confidence must justify the verdict. A model that returns
            # confidence=0.95 with zero facts is hallucinating — skip.
            if fact_count == 0 or not answer:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_unknown_fallback",
                    evidence={
                        "skip_reason": "vision_could_not_extract_facts",
                        "extracted_facts_count": fact_count,
                        "vision_confidence": round(model_conf, 3),
                        "vision_model_used": vc.model,
                        "vision_answer_summary": notes,
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            if model_conf >= 0.85:
                confidence = 0.85
            elif model_conf >= 0.65:
                confidence = 0.65
            else:
                confidence = 0.0

            return ResolverResult(
                resolved=confidence >= 0.85,
                value=(
                    {"answer": answer[:200], "facts": facts}
                    if confidence >= 0.85 else None
                ),
                confidence=confidence,
                method="vision_unknown_fallback",
                evidence={
                    "extracted_facts_count": fact_count,
                    "vision_confidence": round(model_conf, 3),
                    "vision_model_used": vc.model,
                    "vision_answer_summary": notes,
                    **(budget.receipt_evidence() if budget is not None else {}),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("vision_unknown_fallback", exc)


__all__ = ["GeneralVisionResolver"]
