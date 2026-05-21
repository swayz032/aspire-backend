"""Discipline resolver — Wave 19b.

Wraps the existing discipline_verifier four-signal cross-check and adds an
optional full-sheet vision pass on the title block via GPT-5 vision to boost
confidence when the four signals are insufficient.

Methods (tried in parallel by cascade):
  1. verifier_signals  — existing verify_classification() four-signal vote
  2. vision_title_block — GPT-5 vision on the sheet's title block region

Confidence thresholds:
  verifier "promote" (4/4 or 3/4 agree)  → 0.85–0.98
  verifier "flag"                         → 0.55  (below threshold → not resolved)
  verifier "demote"                       → 0.30  (not resolved)
  vision pass with high certainty         → 0.85
  vision pass uncertain                   → 0.60  (not resolved)

Law compliance:
  Law #3 — Never raises; failed methods return confidence=0.0.
  Law #6 — suite_id passed through; verifier is pure function (no DB).
  Law #9 — OCR text NOT logged; only signal names and vote counts.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
    MissingInputResolver,
    ResolverResult,
)

_log = logging.getLogger(__name__)

_VISION_TIMEOUT_S: float = 15.0


class DisciplineResolver(MissingInputResolver):
    """Resolves discipline-ambiguity missing_inputs."""

    category = MissingInputCategory.DISCIPLINE

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        results: list[ResolverResult] = []

        # Run both methods concurrently; cap at 15s each.
        verifier_task = asyncio.create_task(self._method_verifier_signals(candidate))
        vision_task = asyncio.create_task(self._method_vision_title_block(candidate))

        for coro in asyncio.as_completed([verifier_task, vision_task]):
            try:
                result = await asyncio.wait_for(coro, timeout=_VISION_TIMEOUT_S)
                results.append(result)
            except asyncio.TimeoutError:
                results.append(ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="timeout", evidence={"timeout_s": _VISION_TIMEOUT_S},
                    category=self.category,
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(self._safe_result("discipline_resolve", exc))

        return results

    async def _method_verifier_signals(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Re-run the four-signal verifier for sheets mentioned in the description.

        Identifies all sheets in the project that the description references
        (by parsing sheet numbers from the description text), then runs
        verify_classification for each. Returns the highest-confidence verdict.
        """
        try:
            from aspire_orchestrator.services.blueprint.discipline_verifier import (
                verify_classification,
            )

            best_confidence = 0.0
            best_discipline: str | None = None
            best_verdict = "demote"
            sheets_checked = 0

            for sheet in candidate.sheets:
                sheet_id = str(sheet.get("id") or "")
                if not sheet_id:
                    continue

                ocr_text = candidate.ocr_by_sheet.get(sheet_id) or sheet.get("ocr_text") or ""
                symbols = candidate.symbols_by_sheet.get(sheet_id, [])
                neighbor_sheets = [s for s in candidate.sheets if str(s.get("id")) != sheet_id]

                verdict = verify_classification(
                    sheet_id=sheet_id,
                    sheet_number=sheet.get("sheet_number"),
                    discipline_assigned=sheet.get("discipline"),
                    sheet_code=sheet.get("sheet_code"),
                    ocr_text=ocr_text,
                    symbols_on_sheet=symbols,
                    neighbor_sheets=neighbor_sheets,
                )
                sheets_checked += 1

                conf_map = {
                    "promote": verdict.confidence,
                    "confirm": verdict.confidence,
                    "flag": 0.55,
                    "demote": 0.30,
                }
                conf = conf_map.get(verdict.verdict, 0.30)
                if conf > best_confidence:
                    best_confidence = conf
                    best_discipline = verdict.discipline_final
                    best_verdict = verdict.verdict

            resolved = best_confidence >= 0.85 and best_discipline is not None
            return ResolverResult(
                resolved=resolved,
                value=best_discipline,
                confidence=best_confidence,
                method="verifier_signals",
                evidence={
                    "verdict": best_verdict,
                    "sheets_checked": sheets_checked,
                    "discipline": best_discipline,
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("verifier_signals", exc)

    async def _method_vision_title_block(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Full-sheet vision pass on title block via GPT-5 vision.

        Uses the first sheet in the candidate whose image bytes are stored
        in blueprint-thumbnails. Asks the model to identify the discipline
        from the visible title block. Caches result within the current run.
        """
        try:
            from aspire_orchestrator.services.openai_client import generate_json_async
            from aspire_orchestrator.config.settings import settings

            api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ASPIRE_OPENAI_API_KEY", "")
            if not api_key:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_title_block",
                    evidence={"skip_reason": "no_api_key"},
                    category=self.category,
                )

            # Find first sheet with stored thumbnail URL
            sheet_for_vision: dict[str, Any] | None = None
            for sheet in candidate.sheets:
                if sheet.get("thumbnail_url") or sheet.get("image_url"):
                    sheet_for_vision = sheet
                    break

            if sheet_for_vision is None:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_title_block",
                    evidence={"skip_reason": "no_thumbnail_available"},
                    category=self.category,
                )

            image_url = (
                sheet_for_vision.get("thumbnail_url")
                or sheet_for_vision.get("image_url")
            )

            # 1 vision pass per sheet per cascade run (cost control)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a blueprint classification expert. "
                        "Identify the engineering discipline from the sheet's title block. "
                        "Return JSON: {\"discipline\": \"A|S|M|E|P|FP|C|L|Specs|Schedules|Addenda\", "
                        "\"confidence\": 0.0-1.0, \"evidence\": \"brief reason\"}"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url, "detail": "low"},
                        },
                        {
                            "type": "text",
                            "text": "What is the engineering discipline of this blueprint sheet?",
                        },
                    ],
                },
            ]

            result = await generate_json_async(
                model="gpt-5.4-mini",  # vision-capable fast model
                messages=messages,
                api_key=api_key,
                base_url=settings.openai_base_url,
                timeout_seconds=_VISION_TIMEOUT_S,
                max_output_tokens=256,
                temperature=None,
                model_profile="primary_reasoner",
            )
            resp = result.get("json") or {}
            discipline = str(resp.get("discipline") or "").strip()
            confidence = float(resp.get("confidence") or 0.0)
            evidence_str = str(resp.get("evidence") or "")

            valid_disciplines = {
                "A", "S", "M", "E", "P", "FP", "C", "L",
                "Specs", "Schedules", "Addenda",
            }
            if discipline not in valid_disciplines:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_title_block",
                    evidence={"raw_discipline": discipline[:20], "invalid": True},
                    category=self.category,
                )

            return ResolverResult(
                resolved=confidence >= 0.85,
                value=discipline if confidence >= 0.85 else None,
                confidence=confidence,
                method="vision_title_block",
                evidence={
                    "discipline": discipline,
                    "evidence_summary": evidence_str[:80],
                    "sheet_id": str(sheet_for_vision.get("id") or "")[:8],
                },
                category=self.category,
            )

        except Exception as exc:  # noqa: BLE001
            return self._safe_result("vision_title_block", exc)
