"""Dimension resolver — Wave 19b.

Full-sheet vision pass on plan sheets via GPT-5 vision to extract
room-by-room architectural dimensions.

Methods:
  1. vision_plan_sheets — GPT-5 vision structured output with room dimensions.
  2. ocr_dimension_extraction — regex-based extraction from OCR text as fallback.

Confidence:
  Vision with >=3 rooms resolved  → 0.88
  Vision with 1-2 rooms           → 0.75 (below threshold)
  OCR extraction only             → 0.70 (below threshold; supporting evidence)

Law compliance:
  Law #3 — Never raises; low confidence is the signal.
  Law #9 — Room names not logged (potential address info); only counts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
    MissingInputResolver,
    ResolverResult,
)

_log = logging.getLogger(__name__)
_TIMEOUT_S: float = 15.0

# Room dimension patterns in OCR: 10'-6" x 12'-0" or 10.5' x 12'
_ROOM_DIM_PATTERN = re.compile(
    r'(\d{1,3})\s*[\'`-]\s*(?:(\d{1,2})\s*")?\s*[xX]\s*(\d{1,3})\s*[\'`-]\s*(?:(\d{1,2})\s*")?',
)


class DimensionResolver(MissingInputResolver):
    """Resolves missing room-dimension missing_inputs."""

    category = MissingInputCategory.DIMENSION

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        tasks = [
            asyncio.create_task(self._method_vision_plan_sheets(candidate)),
            asyncio.create_task(self._method_ocr_extraction(candidate)),
        ]
        results: list[ResolverResult] = []
        for coro in asyncio.as_completed(tasks):
            try:
                result = await asyncio.wait_for(coro, timeout=_TIMEOUT_S)
                results.append(result)
            except asyncio.TimeoutError:
                results.append(ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="timeout", evidence={"timeout_s": _TIMEOUT_S},
                    category=self.category,
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(self._safe_result("dimension_resolve", exc))
        return results

    async def _method_vision_plan_sheets(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """GPT-5 vision structured pass on architectural plan sheets."""
        try:
            from aspire_orchestrator.services.openai_client import generate_json_async
            from aspire_orchestrator.config.settings import settings

            api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ASPIRE_OPENAI_API_KEY", "")
            if not api_key:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_plan_sheets",
                    evidence={"skip_reason": "no_api_key"},
                    category=self.category,
                )

            # Filter to architectural plan sheets with thumbnails
            plan_sheets = [
                s for s in candidate.sheets
                if str(s.get("discipline") or "").upper() in ("A", "")
                and (s.get("thumbnail_url") or s.get("image_url"))
            ][:3]  # Cap at 3 sheets per cascade run — cost control

            if not plan_sheets:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_plan_sheets",
                    evidence={"skip_reason": "no_plan_sheets_with_thumbnails"},
                    category=self.category,
                )

            all_rooms: list[dict[str, Any]] = []
            sheets_processed = 0

            for sheet in plan_sheets:
                image_url = sheet.get("thumbnail_url") or sheet.get("image_url")
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are a blueprint dimension extractor. "
                            "Extract room dimensions from the architectural plan. "
                            "Return JSON: {\"rooms\": [{\"name\": str, \"width_ft\": float, "
                            "\"depth_ft\": float, \"area_sf\": float}], "
                            "\"confidence\": 0.0-1.0}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url, "detail": "high"},
                            },
                            {
                                "type": "text",
                                "text": "Extract all room dimensions visible in this floor plan.",
                            },
                        ],
                    },
                ]

                result = await generate_json_async(
                    model="gpt-5.4-mini",
                    messages=messages,
                    api_key=api_key,
                    base_url=settings.openai_base_url,
                    timeout_seconds=_TIMEOUT_S,
                    max_output_tokens=1024,
                    temperature=None,
                    model_profile="primary_reasoner",
                )
                resp = result.get("json") or {}
                rooms = resp.get("rooms") or []
                all_rooms.extend(rooms)
                sheets_processed += 1

            if not all_rooms:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_plan_sheets",
                    evidence={"sheets_processed": sheets_processed, "rooms_found": 0},
                    category=self.category,
                )

            confidence = 0.88 if len(all_rooms) >= 3 else 0.75
            return ResolverResult(
                resolved=confidence >= 0.85,
                value=all_rooms,
                confidence=confidence,
                method="vision_plan_sheets",
                evidence={
                    "rooms_found": len(all_rooms),
                    "sheets_processed": sheets_processed,
                },
                category=self.category,
            )

        except Exception as exc:  # noqa: BLE001
            return self._safe_result("vision_plan_sheets", exc)

    async def _method_ocr_extraction(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Extract dimension callouts from OCR text as supporting evidence."""
        try:
            dim_count = 0
            dim_values: list[tuple[float, float]] = []

            for sheet in candidate.sheets:
                if str(sheet.get("discipline") or "").upper() not in ("A", ""):
                    continue
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:4000]

                for m in _ROOM_DIM_PATTERN.finditer(ocr):
                    try:
                        w_ft = float(m.group(1)) + (float(m.group(2) or "0") / 12.0)
                        d_ft = float(m.group(3)) + (float(m.group(4) or "0") / 12.0)
                        if 1.0 <= w_ft <= 100.0 and 1.0 <= d_ft <= 100.0:
                            dim_values.append((w_ft, d_ft))
                            dim_count += 1
                    except (ValueError, IndexError):
                        pass

            if not dim_count:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="ocr_extraction",
                    evidence={"skip_reason": "no_dimension_callouts_found"},
                    category=self.category,
                )

            return ResolverResult(
                resolved=False,  # OCR extraction alone is below threshold
                value=dim_values[:20],  # Cap returned values
                confidence=0.70,
                method="ocr_extraction",
                evidence={"dim_pairs_found": dim_count},
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("ocr_extraction", exc)
