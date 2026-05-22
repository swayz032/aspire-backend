"""Addenda resolver — Wave 19b.

Builds a supersession DAG from addendum cover sheet, sheet index, and
per-sheet revision blocks to resolve addendum/revision hierarchy ambiguity.

Methods:
  1. cover_sheet_index   — parse "Sheet Index" or "Drawing Index" from addendum
                            cover sheet OCR; extract supersession relationships.
  2. revision_block_scan — for each sheet, parse the revision block for Rev/Addendum
                            numbers and their descriptions; detect DAG.
  3. discipline_tagger_style — use discipline_tagger-style keyword matching on
                            addenda-sheet OCR to detect version markers.

Confidence:
  Full DAG resolved (cover + revision blocks agree)  → 0.92
  Partial DAG (revision blocks only)                 → 0.80 (below threshold)
  Only cover sheet index                             → 0.85

Law compliance:
  Law #3 — Never raises; each method returns confidence=0.0 on failure.
  Law #9 — No raw OCR logged; only matched labels and version tokens.
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

# Wave 19.2 (Commit 4) — per-method timeout calibrated against the
# cascade outer cap (90s) AND vision-call latency (gpt-5.4-mini round-trip
# ~10-30s on engineering sheet PNGs). 85s mirrors schedule/scale resolvers.
_TIMEOUT_S: float = 85.0

# Keywords that mark a sheet as a "addendum cover" candidate for vision
# extraction of the supersession list.
_ADDENDUM_COVER_KEYWORDS = re.compile(
    r"\baddend(?:um|a)\b|\brevision\s+(?:log|history)\b|\bsupersess",
    re.IGNORECASE,
)

# Revision block patterns: "Rev 1", "Addendum 2", "ASI-3", "RFI-04"
_REV_PATTERN = re.compile(
    r"(?:rev(?:ision)?|addend(?:um|a)|asi|rfi|bulletin|delta)\s*[-#]?\s*(\d{1,3})",
    re.IGNORECASE,
)
_SUPERSEDES_PATTERN = re.compile(
    r"(?:supersedes?|replaces?|cancels?)\s+([A-Z0-9\-]+)",
    re.IGNORECASE,
)
# Sheet index: "A-101 Architectural Floor Plan Rev 2"
_SHEET_INDEX_LINE = re.compile(
    r"([A-Z]{1,3}-\d{1,4}[A-Za-z]?)\s+.{0,60}\s+(?:rev|addend)\s*(\d{1,3})",
    re.IGNORECASE,
)


class AddendaResolver(MissingInputResolver):
    """Resolves addendum/revision-hierarchy missing_inputs."""

    category = MissingInputCategory.ADDENDA

    async def resolve(self, candidate: MissingInputCandidate) -> list[ResolverResult]:
        tasks = [
            asyncio.create_task(self._method_cover_sheet_index(candidate)),
            asyncio.create_task(self._method_revision_block_scan(candidate)),
            asyncio.create_task(self._method_vision_addendum_cover(candidate)),
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
                results.append(self._safe_result("addenda_resolve", exc))
        return results

    async def _method_cover_sheet_index(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Parse the addendum cover sheet / sheet-index pages for the supersession map."""
        try:
            dag: dict[str, str] = {}  # sheet_code -> latest_rev
            sheets_with_index = 0

            for sheet in candidate.sheets:
                discipline = str(sheet.get("discipline") or "").upper()
                sheet_number = str(sheet.get("sheet_number") or "").upper()
                # Look for cover/index sheets: discipline="Addenda", sheet_number=0 or "G"
                is_index = (
                    "ADDEND" in discipline
                    or sheet_number in ("G-000", "G-001", "T-001", "0", "INDEX")
                    or "INDEX" in str(sheet.get("sheet_title") or "").upper()
                )
                if not is_index:
                    continue

                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:6000]

                for m in _SHEET_INDEX_LINE.finditer(ocr):
                    sheet_code = m.group(1).upper()
                    rev_num = m.group(2)
                    existing = dag.get(sheet_code, "0")
                    if int(rev_num) > int(existing):
                        dag[sheet_code] = rev_num
                    sheets_with_index += 1

            if not dag:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="cover_sheet_index",
                    evidence={"skip_reason": "no_index_sheet_found_or_parseable"},
                    category=self.category,
                )

            confidence = 0.85 if sheets_with_index >= 2 else 0.70
            return ResolverResult(
                resolved=confidence >= 0.85,
                value=dag,
                confidence=confidence,
                method="cover_sheet_index",
                evidence={
                    "sheets_mapped": len(dag),
                    "index_sheets_parsed": sheets_with_index,
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("cover_sheet_index", exc)

    async def _method_revision_block_scan(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Scan revision blocks on each sheet to build version numbers."""
        try:
            rev_by_sheet: dict[str, int] = {}  # sheet_code -> highest rev number
            supersession_pairs: list[tuple[str, str]] = []

            for sheet in candidate.sheets:
                sheet_code = str(sheet.get("sheet_code") or sheet.get("sheet_number") or "")
                if not sheet_code:
                    continue

                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:4000]

                # Find highest revision number on this sheet
                for m in _REV_PATTERN.finditer(ocr):
                    try:
                        rev = int(m.group(1))
                        current = rev_by_sheet.get(sheet_code, 0)
                        if rev > current:
                            rev_by_sheet[sheet_code] = rev
                    except ValueError:
                        pass

                # Find explicit supersession mentions
                for m in _SUPERSEDES_PATTERN.finditer(ocr):
                    supersession_pairs.append((sheet_code, m.group(1).upper()))

            if not rev_by_sheet:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="revision_block_scan",
                    evidence={"skip_reason": "no_revision_blocks_found"},
                    category=self.category,
                )

            sheets_with_revisions = sum(1 for v in rev_by_sheet.values() if v > 0)
            confidence = 0.80 if sheets_with_revisions >= 3 else 0.60

            return ResolverResult(
                resolved=False,  # Partial DAG alone is below threshold; needs cover sheet
                value={"rev_map": rev_by_sheet, "supersessions": supersession_pairs},
                confidence=confidence,
                method="revision_block_scan",
                evidence={
                    "sheets_with_revisions": sheets_with_revisions,
                    "supersession_pairs": len(supersession_pairs),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("revision_block_scan", exc)

    async def _method_vision_addendum_cover(
        self, candidate: MissingInputCandidate
    ) -> ResolverResult:
        """Wave 19.2 / Commit 4 — Vision pass on the addendum cover sheet.

        Picks the sheet whose title/OCR best matches ``_ADDENDUM_COVER_KEYWORDS``
        (preferring index/cover sheets). Falls back to the first sheet in
        the candidate if no match. Sends the PNG to gpt-5.4-mini vision
        with a system prompt that demands strict JSON output enumerating
        which sheet codes are superseded by which addendum.

        Resolves when:
          * 5+ supersession entries returned with model confidence >= 0.80
            OR the count matches the candidate's total sheet count (a
            complete index) — confidence 0.85.
          * 3-4 entries returned with model confidence >= 0.75 — confidence
            0.75 (consensus-eligible, not standalone).

        Skips when:
          * Vision budget exhausted.
          * Model returns < 3 entries, or vague/uncertain output. The
            row stays open for the contractor (no false positive).
        """
        try:
            provider = candidate.data_provider
            budget = candidate.vision_budget

            # ---------- Pick the cover sheet ----------
            cover_sheet: dict[str, Any] | None = None
            for sheet in candidate.sheets:
                title = str(sheet.get("sheet_title") or "")
                ocr = (
                    candidate.ocr_by_sheet.get(str(sheet.get("id") or ""))
                    or sheet.get("ocr_text")
                    or ""
                )[:600]
                if _ADDENDUM_COVER_KEYWORDS.search(title + ocr):
                    cover_sheet = sheet
                    break
            if cover_sheet is None and candidate.sheets:
                cover_sheet = candidate.sheets[0]

            if cover_sheet is None or not cover_sheet.get("id"):
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_addendum_cover",
                    evidence={
                        "skip_reason": "no_cover_sheet_candidate",
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            # ---------- Fetch PNG ----------
            sheet_id = str(cover_sheet["id"])
            png_bytes: bytes | None = None
            if provider is not None:
                try:
                    png_bytes = await provider.get_sheet_png(sheet_id)
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "addenda_resolver.vision: provider fetch failed sheet=%s err=%s",
                        sheet_id[:8], type(exc).__name__,
                    )
            if not png_bytes:
                legacy = cover_sheet.get("image_bytes")
                if isinstance(legacy, (bytes, bytearray)) and legacy:
                    png_bytes = bytes(legacy)

            # ---------- Vision call ----------
            from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
                call_vision_with_budget,
            )
            system_prompt = (
                "You are reading an addendum cover sheet from a construction "
                "drawing set. Return STRICT JSON only — no prose, no markdown.\n"
                "Schema: {\n"
                '  "supersessions": [\n'
                '    {"sheet_code": "<e.g. A-101>", "addendum_number": <int>, '
                '"superseded_by": "<e.g. A-101-R2>"}\n'
                "  ],\n"
                '  "confidence": <0.0 to 1.0>,\n'
                '  "notes": "<one-line evidence summary or empty>"\n'
                "}\n"
                "If you cannot find a concrete supersession list, return "
                '{"supersessions": [], "confidence": 0.0, "notes": "uncertain"}'
                " — do NOT invent entries."
            )
            user_prompt = (
                "Extract the supersession / revision-hierarchy list from this "
                "addendum cover sheet. Only include entries you can read "
                "directly from the sheet. Return JSON per the schema."
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
                    method="vision_addendum_cover",
                    evidence={
                        "skip_reason": vc.skip_reason,
                        "vision_model_used": vc.model,
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            # ---------- Interpret structured output ----------
            entries = vc.json_payload.get("supersessions") or []
            model_conf = float(vc.json_payload.get("confidence") or 0.0)
            entry_count = len(entries) if isinstance(entries, list) else 0
            sheet_count = len(candidate.sheets)

            # No false positives: require concrete entries + model confidence.
            if entry_count < 3 or model_conf < 0.75:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_addendum_cover",
                    evidence={
                        "skip_reason": "vision_could_not_extract_supersession",
                        "supersession_entries_found": entry_count,
                        "vision_confidence": round(model_conf, 3),
                        "vision_model_used": vc.model,
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            # Resolve: 5+ entries OR entries >= sheet_count → 0.85; else 0.75.
            if entry_count >= 5 or (sheet_count > 0 and entry_count >= sheet_count):
                confidence = 0.85
            else:
                confidence = 0.75

            return ResolverResult(
                resolved=confidence >= 0.85,
                value=entries if confidence >= 0.85 else None,
                confidence=confidence,
                method="vision_addendum_cover",
                evidence={
                    "supersession_entries_found": entry_count,
                    "sheets_referenced_in_addendum": entry_count,
                    "vision_confidence": round(model_conf, 3),
                    "vision_model_used": vc.model,
                    **(budget.receipt_evidence() if budget is not None else {}),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("vision_addendum_cover", exc)
