"""Detail resolver — Wave 19b.

Cross-references floor plan callout markers against detail sheets via
lexicon matching on existing OCR text.

Methods:
  1. callout_cross_reference — match callout markers (e.g., "1/A4") on floor
                               plan sheets to detail sheets (e.g., detail 1 on A4).
  2. title_block_detail_index — parse detail index from general-notes/detail sheets.

Confidence:
  >= 5 callouts resolved to detail sheets  → 0.88
  2-4 callouts resolved                    → 0.78 (below threshold)
  0-1 callouts resolved                    → 0.40

Law compliance:
  Law #3 — Never raises; confidence=0.0 is the signal.
  Law #9 — Callout markers (e.g., "1/A4") are safe to use as evidence keys.
"""
from __future__ import annotations

import asyncio
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

# Wave 19.2 (Commit 4) — 85s to accommodate vision call latency.
_TIMEOUT_S: float = 85.0

# Callout marker patterns: "1/A4", "3/S2.1", "TYP/A6"
_CALLOUT_PATTERN = re.compile(
    r"\b(\d{1,2}|[A-Z]{1,3})\s*/\s*([A-Z]{1,2}\d{1,3}(?:\.\d{1,2})?)\b",
)
# Detail sheet title indicators
_DETAIL_SHEET_INDICATOR = re.compile(
    r"\bdetail(?:s)?\b|\bsection(?:s)?\b|\bcallout(?:s)?\b",
    re.IGNORECASE,
)


class DetailResolver(MissingInputResolver):
    """Resolves door/partition/detail-reference missing_inputs."""

    category = MissingInputCategory.DETAIL

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        tasks = [
            asyncio.create_task(self._method_callout_cross_reference(candidate)),
            asyncio.create_task(self._method_vision_callout_detail(candidate)),
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
                results.append(self._safe_result("detail_resolve", exc))
        return results

    async def _method_callout_cross_reference(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Match callout markers on plan sheets to detail sheets."""
        try:
            # Build a set of known detail-sheet codes: e.g., "A4", "S2.1"
            detail_sheet_codes: set[str] = set()
            for sheet in candidate.sheets:
                sheet_code = str(sheet.get("sheet_code") or "")
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )
                if _DETAIL_SHEET_INDICATOR.search(
                    str(sheet.get("sheet_title") or "")
                ) or _DETAIL_SHEET_INDICATOR.search(ocr[:500]):
                    if sheet_code:
                        detail_sheet_codes.add(sheet_code.upper())

            # Scan plan sheets for callout markers
            callouts_found: list[str] = []
            callouts_resolved: list[str] = []

            for sheet in candidate.sheets:
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:4000]
                for m in _CALLOUT_PATTERN.finditer(ocr):
                    detail_num = m.group(1)
                    sheet_ref = m.group(2).upper()
                    callout_str = f"{detail_num}/{sheet_ref}"
                    callouts_found.append(callout_str)
                    if sheet_ref in detail_sheet_codes:
                        callouts_resolved.append(callout_str)

            resolved_count = len(callouts_resolved)
            if resolved_count == 0 and not callouts_found:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="callout_cross_reference",
                    evidence={"skip_reason": "no_callout_markers_found"},
                    category=self.category,
                )

            if resolved_count >= 5:
                confidence = 0.88
            elif resolved_count >= 2:
                confidence = 0.78
            else:
                confidence = 0.40

            return ResolverResult(
                resolved=confidence >= 0.85,
                value=callouts_resolved[:20] if confidence >= 0.85 else None,
                confidence=confidence,
                method="callout_cross_reference",
                evidence={
                    "callouts_found": len(callouts_found),
                    "callouts_resolved": resolved_count,
                    "detail_sheets_identified": len(detail_sheet_codes),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("callout_cross_reference", exc)

    async def _method_vision_callout_detail(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Wave 19.2 / Commit 4 — Vision pass on detail-sheet target regions.

        Parses callout markers from BOTH the candidate's description AND the
        OCR text of every sheet (matches "1/A4", "3/S2.1", etc). For each
        unique callout, locates the target sheet by ``sheet_code``, fetches
        the PNG, and asks gpt-5.4-mini vision: "describe detail #{n} on this
        sheet". Strict JSON output: label + dimensions OR materials.

        Resolves when 2+ callouts return concrete structured answers with
        model confidence >= 0.75 (confidence 0.85). Skips when zero
        callouts can be located or every model response is vague.

        Cost guard: at most ``_MAX_CALLOUTS_PER_CASCADE`` (3) vision calls
        per cascade-run to keep cost bounded even before the global
        VisionBudget kicks in. The budget still wraps every call so multi-
        cascade backfills can't overrun.
        """
        try:
            provider = candidate.data_provider
            budget = candidate.vision_budget

            # ---------- Parse callouts from description + sheet OCR ----------
            seen_callouts: set[tuple[str, str]] = set()  # (detail_num, sheet_ref)
            for source in [candidate.description] + [
                (candidate.ocr_by_sheet.get(str(s.get("id") or ""))
                 or s.get("ocr_text") or "")[:4000]
                for s in candidate.sheets
            ]:
                for m in _CALLOUT_PATTERN.finditer(source):
                    seen_callouts.add((m.group(1), m.group(2).upper()))

            callouts_parsed = len(seen_callouts)
            if callouts_parsed == 0:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_callout_detail",
                    evidence={
                        "skip_reason": "no_callouts_in_description_or_ocr",
                        "callouts_parsed_from_description": 0,
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            # ---------- Cap callouts to keep cost bounded ----------
            _MAX_CALLOUTS_PER_CASCADE = 3
            callouts_to_try = list(seen_callouts)[:_MAX_CALLOUTS_PER_CASCADE]

            # ---------- Index sheets by sheet_code for O(1) lookup ----------
            sheets_by_code: dict[str, dict[str, Any]] = {
                str(s.get("sheet_code") or "").upper(): s
                for s in candidate.sheets
                if s.get("sheet_code")
            }

            # ---------- Vision call per callout (sequential to honour budget) ----------
            from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
                call_vision_with_budget,
                run_azure_layout_pre_pass,
            )
            resolved_callouts: list[dict[str, Any]] = []
            callouts_attempted = 0
            azure_pre_passes_used = 0
            highest_conf = 0.0
            last_model = ""

            for detail_num, sheet_ref in callouts_to_try:
                target_sheet = sheets_by_code.get(sheet_ref)
                if target_sheet is None:
                    continue
                sheet_id = str(target_sheet.get("id") or "")
                if not sheet_id:
                    continue

                png_bytes: bytes | None = None
                if provider is not None:
                    try:
                        png_bytes = await provider.get_sheet_png(sheet_id)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "detail_resolver.vision: provider fetch failed sheet=%s err=%s",
                            sheet_id[:8], type(exc).__name__,
                        )
                if not png_bytes:
                    legacy = target_sheet.get("image_bytes")
                    if isinstance(legacy, (bytes, bytearray)) and legacy:
                        png_bytes = bytes(legacy)

                # Wave 19.2.1 — Azure layout pre-pass for each target sheet.
                # Detail callouts are usually referenced FROM schedules
                # (door schedule, partition schedule), so structured cells
                # are the right interpretation source.
                azure_layout = await run_azure_layout_pre_pass(
                    png_bytes=png_bytes,
                    suite_id=candidate.suite_id,
                    office_id=candidate.office_id or "",
                )
                if azure_layout is not None:
                    azure_pre_passes_used += 1

                system_prompt = (
                    "You are reading a construction-drawing detail sheet. "
                    "Return STRICT JSON only.\n"
                    "Schema: {\n"
                    '  "detail_label": "<short label e.g. INTERIOR PARTITION TYPE 1>",\n'
                    '  "dimensions": {"<measure>": "<value>"},\n'
                    '  "materials_called_out": ["<material 1>", ...],\n'
                    '  "confidence": <0.0 to 1.0>,\n'
                    '  "notes": "<one-line evidence summary>"\n'
                    "}\n"
                    "If you cannot find the requested detail on this sheet, "
                    'return {"detail_label": "", "dimensions": {}, '
                    '"materials_called_out": [], "confidence": 0.0, '
                    '"notes": "not found"} — do NOT invent.'
                )
                user_prompt = (
                    f"Find detail #{detail_num} on this sheet "
                    f"(callout reference {detail_num}/{sheet_ref}). "
                    "Use the structured layout above (if present) as primary "
                    "source — detail labels and dimensions are usually in "
                    "schedule cells. Describe what the detail shows per the "
                    "JSON schema."
                )
                vc = await call_vision_with_budget(
                    png_bytes=png_bytes,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    vision_budget=budget,
                    extracted_layout=azure_layout,
                    max_output_tokens=500,
                    timeout_seconds=60.0,
                )
                callouts_attempted += 1
                last_model = vc.model
                if not vc.ok:
                    # Budget exhausted is the only reason to stop the loop;
                    # other skip reasons mean this callout failed but the
                    # next might work.
                    if vc.skip_reason == "vision_budget_exhausted":
                        break
                    continue

                payload = vc.json_payload
                label = str(payload.get("detail_label") or "").strip()
                dims = payload.get("dimensions") or {}
                materials = payload.get("materials_called_out") or []
                model_conf = float(payload.get("confidence") or 0.0)

                # No-false-positives gate: require concrete extraction.
                has_label = bool(label) and label.lower() != "not found"
                has_dims = isinstance(dims, dict) and len(dims) >= 1
                has_materials = (
                    isinstance(materials, list) and len(materials) >= 1
                )
                if model_conf < 0.75 or not has_label or not (has_dims or has_materials):
                    continue

                resolved_callouts.append({
                    "callout": f"{detail_num}/{sheet_ref}",
                    "detail_label": label[:80],
                    "dimensions_count": len(dims) if isinstance(dims, dict) else 0,
                    "materials_count": len(materials) if isinstance(materials, list) else 0,
                })
                if model_conf > highest_conf:
                    highest_conf = model_conf

            resolved_count = len(resolved_callouts)
            if resolved_count >= 2:
                confidence = 0.85
            elif resolved_count == 1:
                confidence = 0.75
            else:
                confidence = 0.0

            return ResolverResult(
                resolved=confidence >= 0.85,
                value=resolved_callouts if confidence >= 0.85 else None,
                confidence=confidence,
                method="vision_callout_detail",
                evidence={
                    "callouts_parsed_from_description": callouts_parsed,
                    "callouts_vision_resolved": resolved_count,
                    "vision_confidence": round(highest_conf, 3),
                    "vision_model_used": last_model or "gpt-5.4-mini",
                    "azure_layout_pre_passes_used": azure_pre_passes_used,
                    **(budget.receipt_evidence() if budget is not None else {}),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("vision_callout_detail", exc)
