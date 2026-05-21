"""SheetDataProvider — Wave 19.1a foundation.

Read-only data plane that gives missing-input resolvers access to SEE-stage
artifacts (PNG bytes from Supabase Storage, YOLO detections from the
`blueprint_symbols` table). One instance per cascade run; reads are cached
in-memory for the lifetime of that instance so a single cascade pass does
not hit Storage or Postgres twice for the same sheet.

Why this exists
---------------
Before Wave 19.1, resolvers operated only on the per-row fields that
`drew_blueprint._run_missing_input_cascade` loaded from `blueprint_sheets`
(notably `ocr_text`). The resolvers' `_method_*` implementations that
reference `sheet["image_bytes"]` (see `scale_resolver.py:96` and
`schedule_resolver.py:106`) therefore always saw an empty value, and any
PNG-driven method silently degraded. This provider is the supply layer
those methods need — wire-up happens in Commits 2 (schedule) and 3 (scale).

Scope of Commit 1 (THIS commit)
-------------------------------
- `get_sheet_png` — cached download from Supabase Storage via the existing
  `sheet_image_storage.download_sheet_image` helper.
- `get_yolo_detections` — cached query against `blueprint_symbols`.

Out of scope (deferred until upstream recon completes)
------------------------------------------------------
- Legend extraction. `legend_anchor_v2.extract_legend_anchors` requires a
  live `fitz.Page` plus pre-extracted vector primitives and a motif builder.
  PNG bytes alone are not enough. Wiring this needs a separate decision on
  whether to re-open the PDF in the provider or persist legend anchors to
  a new table during SEE. Returns `None` today; callers must treat that as
  "not available".
- Spatial-OCR bindings. `spatial_ocr_binder.bind_symbols_to_ocr` requires
  `list[SymbolBox]` and `list[OcrWord]` — typed structures that are not
  persisted today. Same deferral as legend.

Both deferred methods exist as typed stubs so resolvers can rely on a
stable provider interface and we don't ship a half-finished mock that
returns the wrong shape.

Law compliance
--------------
- Law #1 — Provider returns data; never decides or persists.
- Law #2 — Provider does not emit receipts. The cascade owns receipt
  emission per missing-input resolution attempt.
- Law #6 — All reads scoped by `suite_id`. Storage paths use the
  `{suite_id}/{project_id}/{sheet_id}.png` prefix enforced by
  `sheet_image_storage`. The YOLO query filters on both `sheet_id` AND
  `suite_id`.
- Law #9 — No raw PNG bytes or OCR text logged. Only sheet UUID prefixes,
  byte lengths, and counts.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


class SheetDataProvider:
    """Per-cascade-run cache + accessor for SEE-stage sheet artifacts.

    Construct ONE instance per cascade dispatch (i.e. once inside
    `_run_missing_input_cascade`) and pass it via `MissingInputCandidate.data_provider`
    to every candidate built for that dispatch. The instance is not safe
    to share across cascade runs because its caches accumulate without
    bound — discard it when the dispatch completes.
    """

    def __init__(
        self,
        suite_id: str,
        project_id: str,
        correlation_id: str,
    ) -> None:
        if not suite_id:
            raise ValueError("SheetDataProvider requires a non-empty suite_id (Law #6)")
        if not project_id:
            raise ValueError("SheetDataProvider requires a non-empty project_id")
        self._suite_id: str = suite_id
        self._project_id: str = project_id
        self._correlation_id: str = correlation_id or ""

        # Per-instance caches. Keys are sheet_id strings.
        self._png_cache: dict[str, bytes | None] = {}
        self._yolo_cache: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Identity (used by resolvers / tests to assert scoping)
    # ------------------------------------------------------------------

    @property
    def suite_id(self) -> str:
        return self._suite_id

    @property
    def project_id(self) -> str:
        return self._project_id

    # ------------------------------------------------------------------
    # PNG bytes (Supabase Storage)
    # ------------------------------------------------------------------

    async def get_sheet_png(self, sheet_id: str) -> bytes | None:
        """Return PNG bytes for ``sheet_id`` from Supabase Storage.

        Cached per-instance: a second call for the same sheet returns the
        first call's result (including ``None`` for cache miss) without
        re-hitting Storage.

        Returns ``None`` on any failure (cache miss, network error, empty
        sheet_id). Never raises.
        """
        if not sheet_id:
            return None

        if sheet_id in self._png_cache:
            return self._png_cache[sheet_id]

        try:
            from aspire_orchestrator.services.blueprint.sheet_image_storage import (
                download_sheet_image,
            )
            png = await download_sheet_image(
                suite_id=self._suite_id,
                project_id=self._project_id,
                sheet_id=sheet_id,
                correlation_id=self._correlation_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "sheet_data_provider.get_sheet_png: %s sheet=%s",
                type(exc).__name__,
                sheet_id[:8] if sheet_id else "",
            )
            png = None

        self._png_cache[sheet_id] = png
        return png

    # ------------------------------------------------------------------
    # YOLO detections (blueprint_symbols)
    # ------------------------------------------------------------------

    async def get_yolo_detections(self, sheet_id: str) -> list[dict[str, Any]]:
        """Return YOLO detections for ``sheet_id`` from ``blueprint_symbols``.

        Each detection dict has at minimum: ``id``, ``sheet_id``, ``class``,
        ``bbox`` (jsonb), ``confidence``. RLS-scoped by ``suite_id`` AND
        ``sheet_id`` in the filter (defense in depth — RLS would catch a
        cross-tenant read but we also gate explicitly).

        Returns ``[]`` on any failure or empty result. Never raises.
        """
        if not sheet_id:
            return []

        if sheet_id in self._yolo_cache:
            return self._yolo_cache[sheet_id]

        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            rows = await supabase_select(
                "blueprint_symbols",
                filters=f"sheet_id=eq.{sheet_id}&suite_id=eq.{self._suite_id}",
            )
            detections: list[dict[str, Any]] = list(rows or [])
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "sheet_data_provider.get_yolo_detections: %s sheet=%s",
                type(exc).__name__,
                sheet_id[:8] if sheet_id else "",
            )
            detections = []

        self._yolo_cache[sheet_id] = detections
        return detections

    # ------------------------------------------------------------------
    # Deferred — see module docstring "Out of scope" section
    # ------------------------------------------------------------------

    async def get_legend(
        self,
        page_num: int,
        discipline: str | None = None,
    ) -> list[Any] | None:
        """Return legend anchors for a page. Deferred — see module docstring.

        Wave 19.1a ships this as a typed stub returning ``None`` so resolvers
        can rely on the interface shape. Wiring requires either
        re-opening the PDF in-process (heavy) or persisting legend anchors
        to a new table during SEE. That decision is out of scope for this
        commit.
        """
        del page_num, discipline  # explicit no-op; suppress unused-arg lint
        return None

    async def get_spatial_ocr_bindings(self, sheet_id: str) -> dict[str, Any] | None:
        """Return spatial-OCR bindings for ``sheet_id``. Deferred.

        ``spatial_ocr_binder.bind_symbols_to_ocr`` requires typed
        ``SymbolBox`` and ``OcrWord`` lists that are not persisted today.
        Returns ``None`` until the upstream persistence question is
        answered.
        """
        del sheet_id
        return None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def cache_stats(self) -> dict[str, int]:
        """Return a small diag summary for receipts/logs.

        Safe to log — no PII, no raw bytes, only counts.
        """
        return {
            "png_cache_entries": len(self._png_cache),
            "png_cache_hits_with_bytes": sum(
                1 for v in self._png_cache.values() if v
            ),
            "yolo_cache_entries": len(self._yolo_cache),
            "yolo_detections_total": sum(
                len(v) for v in self._yolo_cache.values()
            ),
        }


__all__ = ["SheetDataProvider"]
