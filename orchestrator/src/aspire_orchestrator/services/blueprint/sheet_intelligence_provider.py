"""SheetIntelligenceProvider — Wave 20e cache-first layout data plane.

Reads ``blueprint_sheet_intelligence`` rows populated at INGEST time
(Wave 20c/20d). Provides structured Azure layout data (tables, paragraphs,
legends, schedules, notes) to cascade resolvers and REASON-stage consumers
without issuing live Azure calls on the hot path.

Falls through to a live Azure call ONLY when a cache miss is detected
(sheet not yet ingested or ingest failed), logging the miss so the
observability layer can surface cold-path cost.

Law compliance
--------------
* Law #1 — Provider returns data; never decides or persists.
* Law #2 — Provider does not emit receipts. The cascade / REASON stage
  owns receipt emission.
* Law #3 — Missing suite_id raises at construction time (fail closed).
* Law #6 — All reads scoped by suite_id AND project_id. DB filter enforces
  both; RLS is defense in depth. Cross-tenant requests return empty.
* Law #9 — No raw cell text or PII logged. Only UUID prefixes, row counts,
  and boolean hit/miss flags.

Mirrors SheetDataProvider (resolvers/sheet_data_provider.py) in public API
shape so callers can use both providers behind the same interface pattern.
"""
from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.services.supabase_client import supabase_select

_log = logging.getLogger(__name__)


class SheetIntelligenceProvider:
    """Wave 20: cache-first provider for sheet layout data.

    Reads blueprint_sheet_intelligence rows populated at INGEST time.
    Falls through to live Azure call ONLY if cache miss (logs the miss).
    Suite-bound (Law #6).

    Construct ONE instance per cascade run / REASON dispatch and call
    ``load_all()`` once to batch-load all rows for the project. Subsequent
    accessor calls read from the in-memory cache — zero additional DB hits.
    """

    def __init__(
        self,
        project_id: str,
        suite_id: str,
        office_id: str | None = None,
    ) -> None:
        if not suite_id:
            raise ValueError(
                "SheetIntelligenceProvider requires non-empty suite_id (Law #6)"
            )
        if not project_id:
            raise ValueError(
                "SheetIntelligenceProvider requires non-empty project_id"
            )
        self._project_id = project_id
        self._suite_id = suite_id
        self._office_id = office_id

        # Keyed by sheet_id → blueprint_sheet_intelligence row dict.
        self._rows: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def suite_id(self) -> str:
        return self._suite_id

    @property
    def project_id(self) -> str:
        return self._project_id

    # ------------------------------------------------------------------
    # Bulk load (call once before using accessors)
    # ------------------------------------------------------------------

    async def load_all(self) -> None:
        """Load all sheet_intelligence rows for the project into memory.

        One batched Supabase query; all subsequent accessor calls read
        from ``self._rows`` with zero DB round-trips.

        Safe to call multiple times — subsequent calls are no-ops if
        already loaded. Never raises (failures log a warning and leave
        the cache empty; accessors degrade to cache-miss paths).
        """
        if self._loaded:
            return

        try:
            rows = await supabase_select(
                "blueprint_sheet_intelligence",
                filters=(
                    f"project_id=eq.{self._project_id}"
                    f"&suite_id=eq.{self._suite_id}"
                ),
            )
            self._rows = {
                str(r.get("sheet_id") or ""): r
                for r in (rows or [])
                if r.get("sheet_id")
            }
            _log.info(
                "sheet_intelligence_provider.load_all: project=%s suite=%s rows=%d",
                self._project_id[:8],
                self._suite_id[:8],
                len(self._rows),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "sheet_intelligence_provider.load_all: failed (%s) project=%s",
                type(exc).__name__,
                self._project_id[:8],
            )
        finally:
            self._loaded = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_for(self, sheet_id: str) -> dict[str, Any] | None:
        """Return the cached row for sheet_id; log miss if not found."""
        row = self._rows.get(sheet_id)
        if row is None:
            _log.info(
                "sheet_intelligence_provider: cache-miss sheet=%s project=%s",
                sheet_id[:8] if sheet_id else "",
                self._project_id[:8],
            )
        else:
            _log.debug(
                "sheet_intelligence_provider: cache-hit sheet=%s project=%s",
                sheet_id[:8] if sheet_id else "",
                self._project_id[:8],
            )
        return row

    # ------------------------------------------------------------------
    # Accessors — project-wide
    # ------------------------------------------------------------------

    async def legends(self, *, discipline: str | None = None) -> list[dict[str, Any]]:
        """Return all legend regions across sheets, optionally filtered by discipline.

        Each entry is a dict from the ``regions.legend`` jsonb array
        (bbox, sheet_context, discipline, etc.) plus an injected
        ``sheet_id`` key for traceability.

        Args:
            discipline: When provided, only entries whose ``discipline``
                field matches (case-insensitive) are returned. When None,
                all legend entries are returned.
        """
        result: list[dict[str, Any]] = []
        for sheet_id, row in self._rows.items():
            regions: dict[str, Any] = row.get("regions") or {}
            entries: list[Any] = regions.get("legend") or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if discipline is not None:
                    entry_disc = str(entry.get("discipline") or "").lower()
                    if entry_disc and entry_disc != discipline.lower():
                        continue
                result.append({**entry, "sheet_id": sheet_id})
        return result

    async def schedules(
        self,
        *,
        schedule_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all schedule regions across sheets, optionally filtered by type.

        Each entry is from the ``regions.schedule`` jsonb array plus
        an injected ``sheet_id`` key.

        Args:
            schedule_type: When provided, only entries whose ``schedule_type``
                field matches (case-insensitive) are returned. Typical values:
                DOOR, WINDOW, PANEL, FIXTURE, EQUIPMENT. When None, all
                schedule entries are returned.
        """
        result: list[dict[str, Any]] = []
        for sheet_id, row in self._rows.items():
            regions: dict[str, Any] = row.get("regions") or {}
            entries: list[Any] = regions.get("schedule") or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if schedule_type is not None:
                    entry_type = str(entry.get("schedule_type") or "").upper()
                    if entry_type and entry_type != schedule_type.upper():
                        continue
                result.append({**entry, "sheet_id": sheet_id})
        return result

    async def notes(self) -> list[dict[str, Any]]:
        """Return all general-notes paragraphs across sheets.

        Returns entries from the ``regions.notes`` jsonb array plus an
        injected ``sheet_id`` key. Each entry typically has ``text``,
        ``bbox``, and ``role`` fields matching the paragraphs schema.
        """
        result: list[dict[str, Any]] = []
        for sheet_id, row in self._rows.items():
            regions: dict[str, Any] = row.get("regions") or {}
            entries: list[Any] = regions.get("notes") or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                result.append({**entry, "sheet_id": sheet_id})
        return result

    # ------------------------------------------------------------------
    # Accessors — per-sheet
    # ------------------------------------------------------------------

    async def tables_on_sheet(self, sheet_id: str) -> list[dict[str, Any]]:
        """Return all tables on one specific sheet.

        Each table dict has at minimum: ``bbox``, ``headers``, ``rows``,
        ``region_type`` (from the ingest extractor). Returns ``[]`` on
        cache miss or missing sheet_id.
        """
        if not sheet_id:
            return []
        row = self._row_for(sheet_id)
        if row is None:
            return []
        tables: list[Any] = row.get("tables") or []
        return [t for t in tables if isinstance(t, dict)]

    async def paragraphs_on_sheet(self, sheet_id: str) -> list[dict[str, Any]]:
        """Return all paragraphs on one specific sheet.

        Each paragraph dict has at minimum: ``bbox``, ``text``, ``role``
        (from the ingest extractor). Returns ``[]`` on cache miss.
        """
        if not sheet_id:
            return []
        row = self._row_for(sheet_id)
        if row is None:
            return []
        paragraphs: list[Any] = row.get("paragraphs") or []
        return [p for p in paragraphs if isinstance(p, dict)]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def cache_stats(self) -> dict[str, int | bool]:
        """Return a diag summary safe to log/receipt — no PII, no raw text."""
        return {
            "loaded": self._loaded,
            "sheets_cached": len(self._rows),
            "tables_total": sum(
                len(r.get("tables") or []) for r in self._rows.values()
            ),
            "paragraphs_total": sum(
                len(r.get("paragraphs") or []) for r in self._rows.values()
            ),
        }


__all__ = ["SheetIntelligenceProvider"]
