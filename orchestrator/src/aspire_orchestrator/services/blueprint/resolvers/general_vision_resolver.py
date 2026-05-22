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
        # Wave 19.2.1 — Pick the target sheet ONCE, run Azure
        # prebuilt-layout ONCE, then run both methods against the same
        # ``analyzeResult``:
        #   1. _method_vision_unknown_fallback — vision interpretation
        #      with Azure layout as structured context (hybrid).
        #   2. _method_azure_layout_only — pure cell-keyword search that
        #      surfaces numeric facts even when vision returns "unclear".
        # Sharing one Azure call across both methods keeps cost bounded
        # at ~$0.01-0.03 per cascade (Azure-only) + vision-budget.
        target_sheet, png_bytes = await self._pick_target_sheet_and_png(candidate)
        if target_sheet is None or not png_bytes:
            budget = candidate.vision_budget
            return [ResolverResult(
                resolved=False, value=None, confidence=0.0,
                method="vision_unknown_fallback",
                evidence={
                    "skip_reason": "no_target_sheet_available",
                    **(budget.receipt_evidence() if budget is not None else {}),
                },
                category=self.category,
            )]

        from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
            run_azure_layout_pre_pass,
        )
        azure_layout = await run_azure_layout_pre_pass(
            png_bytes=png_bytes,
            suite_id=candidate.suite_id,
            office_id=candidate.office_id or "",
        )

        vision_result = await self._method_vision_unknown_fallback(
            candidate, png_bytes=png_bytes, azure_layout=azure_layout,
        )
        azure_only_result = self._method_azure_layout_only(
            candidate, azure_layout=azure_layout,
        )
        return [vision_result, azure_only_result]

    async def _pick_target_sheet_and_png(
        self, candidate: MissingInputCandidate,
    ) -> tuple[dict[str, Any] | None, bytes | None]:
        """Pick the most-referenced sheet from the description, fetch PNG.

        Returns ``(target_sheet, png_bytes)``. Either may be ``None`` if
        no usable sheet exists or PNG fetch fails for all sources.
        """
        provider = candidate.data_provider

        referenced_codes = [
            m.group(1).upper().replace("-", "")
            for m in _SHEET_REF_PATTERN.finditer(candidate.description or "")
        ]
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
            target_sheet = candidate.sheets[-1]

        if target_sheet is None or not target_sheet.get("id"):
            return (None, None)

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

        return (target_sheet, png_bytes)

    async def _method_vision_unknown_fallback(
        self,
        candidate: MissingInputCandidate,
        *,
        png_bytes: bytes | None = None,
        azure_layout: dict[str, Any] | None = None,
    ) -> ResolverResult:
        """Vision pass on the most-referenced sheet with the literal description prompt.

        Wave 19.2.1: ``png_bytes`` and ``azure_layout`` are pre-fetched by
        ``resolve()`` so this method and ``_method_azure_layout_only``
        share the same Azure call. When called standalone (older test
        fixtures), both args default to None and we fall back to fetching
        them inline.
        """
        try:
            budget = candidate.vision_budget

            # Standalone-call back-compat: fetch PNG + Azure layout if
            # caller (older test) didn't pre-fetch.
            if png_bytes is None:
                _ts, png_bytes = await self._pick_target_sheet_and_png(candidate)
            if png_bytes is None:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="vision_unknown_fallback",
                    evidence={
                        "skip_reason": "no_target_sheet_available",
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            azure_layout_used = bool(azure_layout)
            azure_tables_count = (
                len((azure_layout or {}).get("tables") or [])
                if azure_layout_used else 0
            )

            # ---------- Vision call ----------
            from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
                call_vision_with_budget,
            )
            # Wave 19.2.1 — legend-aware prompt. The Gulfstream sheet 39
            # diagnostic showed the model returning "unclear" while the
            # PNG literally contained a sprinkler LEGEND table with
            # ``NET TOTAL: 33``. Explicit instructions to extract numbers
            # from tables / legends, even when fine print is hard to
            # read on the PNG, because Azure has already extracted them
            # for us in the structured layout above.
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
                "  * Construction drawings have LEGENDS and TABLES (usually "
                "    in corners or right margin). If you see ANY table or "
                "    legend with numeric cells related to the question, "
                "    EXTRACT THE NUMBERS — do NOT say 'unclear' just because "
                "    the PNG is small. The structured Azure layout above "
                "    contains direct cell-level OCR; trust those cells over "
                "    your visual interpretation when they disagree.\n"
                "  * Only return facts you can READ from the sheet (PNG or "
                "    structured layout). \n"
                "  * If neither the PNG nor the layout contains the answer, "
                "    return "
                '"answer": "", "extracted_facts": [], "confidence": 0.0\n'
                "  * Do NOT estimate or extrapolate. No false answers."
            )
            user_prompt = (
                "The contractor's question is reproduced verbatim below. "
                "Use the structured Azure layout above as your PRIMARY source "
                "(it contains direct cell-level OCR). Use the PNG only to "
                "disambiguate or confirm. Answer using ONLY what is in either.\n\n"
                f"Question: {candidate.description[:500]}"
            )
            vc = await call_vision_with_budget(
                png_bytes=png_bytes,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                vision_budget=budget,
                extracted_layout=azure_layout,
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
                        "azure_layout_pre_pass_used": azure_layout_used,
                        "azure_layout_tables_count": azure_tables_count,
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
                        "azure_layout_pre_pass_used": azure_layout_used,
                        "azure_layout_tables_count": azure_tables_count,
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
                    "azure_layout_pre_pass_used": azure_layout_used,
                    "azure_layout_tables_count": azure_tables_count,
                    **(budget.receipt_evidence() if budget is not None else {}),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("vision_unknown_fallback", exc)

    def _method_azure_layout_only(
        self,
        candidate: MissingInputCandidate,
        *,
        azure_layout: dict[str, Any] | None,
    ) -> ResolverResult:
        """Wave 19.2.1 — pure Azure layout fallback (no vision call).

        Scans the Azure ``analyzeResult`` tables for cells whose text
        matches keywords from ``candidate.description``. If a table
        contains both:
          * a cell matching a question keyword (sprinkler / head / count /
            fixture / panel / circuit / etc.), AND
          * one or more cells with numeric content,
        emit those numeric facts at confidence 0.85 (resolves).

        No-false-positives gate: we require BOTH a keyword match AND a
        numeric cell. A purely numeric table with no question-relevant
        label does NOT resolve — there's nothing to anchor the number
        against. Pure synchronous function (no I/O): Azure already ran.

        Returns confidence 0.0 with ``skip_reason`` evidence when the
        layout is empty / no matching cells exist / no numerics found.
        """
        try:
            budget = candidate.vision_budget
            tables = (azure_layout or {}).get("tables") or []
            if not tables:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="azure_layout_only",
                    evidence={
                        "skip_reason": "azure_layout_unavailable_or_empty",
                        "azure_layout_tables_count": 0,
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            # Tokenize the description into lower-cased word stems.
            # Keep only words length >= 4 to avoid matching "is", "the", etc.
            keywords = {
                w.lower().strip(".,:;")
                for w in (candidate.description or "").split()
                if len(w.strip(".,:;")) >= 4
            }
            # Also include domain-specific short tokens we always want to match.
            domain_anchors = {
                "head", "heads", "fire", "ef", "rtu", "ahu", "vav",
            }
            keywords |= domain_anchors

            extracted_facts: list[dict[str, Any]] = []
            keyword_hits = 0
            tables_with_match = 0

            for ti, table in enumerate(tables):
                if not isinstance(table, dict):
                    continue
                cells = table.get("cells") or []
                table_keyword_cells: list[str] = []
                table_numeric_cells: list[str] = []
                for cell in cells:
                    if not isinstance(cell, dict):
                        continue
                    text = str(cell.get("content") or "").strip()
                    if not text:
                        continue
                    text_low = text.lower()
                    # Keyword match if any keyword appears in the cell.
                    if any(kw in text_low for kw in keywords):
                        table_keyword_cells.append(text[:80])
                        keyword_hits += 1
                    # Numeric cell: contains at least one digit AND is
                    # short enough to be a count / value (not paragraph text).
                    has_digit = any(c.isdigit() for c in text)
                    if has_digit and len(text) <= 40:
                        table_numeric_cells.append(text[:40])

                if table_keyword_cells and table_numeric_cells:
                    tables_with_match += 1
                    # Surface up to 5 numeric facts per matching table.
                    for num_text in table_numeric_cells[:5]:
                        extracted_facts.append({
                            "fact_type": "azure_table_cell",
                            "value": num_text,
                            "table_index": ti,
                            "keyword_cell_sample": (
                                table_keyword_cells[0][:60]
                                if table_keyword_cells else ""
                            ),
                        })

            if not extracted_facts:
                return ResolverResult(
                    resolved=False, value=None, confidence=0.0,
                    method="azure_layout_only",
                    evidence={
                        "skip_reason": (
                            "no_keyword_numeric_match"
                            if keyword_hits > 0
                            else "no_keyword_match_in_tables"
                        ),
                        "azure_layout_tables_count": len(tables),
                        "azure_table_keyword_hits": keyword_hits,
                        **(budget.receipt_evidence() if budget is not None else {}),
                    },
                    category=self.category,
                )

            # We have keyword+numeric pairs — resolve at 0.85.
            return ResolverResult(
                resolved=True,
                value={
                    "facts": extracted_facts[:10],  # cap value payload
                },
                confidence=0.85,
                method="azure_layout_only",
                evidence={
                    "azure_layout_tables_count": len(tables),
                    "azure_table_keyword_hits": keyword_hits,
                    "azure_tables_with_keyword_numeric_match": tables_with_match,
                    "extracted_facts_count": len(extracted_facts),
                    **(budget.receipt_evidence() if budget is not None else {}),
                },
                category=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            return self._safe_result("azure_layout_only", exc)


__all__ = ["GeneralVisionResolver"]
