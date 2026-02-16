"""Receipt Reconciliation — Compare in-memory vs Supabase receipt stores (Law #2).

Ensures 100% receipt coverage by detecting gaps between the in-memory
receipt store and the Supabase dual-write target. Gaps indicate either:
  1. Supabase write failure (needs replay)
  2. In-memory receipt lost (needs investigation)
  3. Race condition (needs retry)

Per Law #2: Every state change produces an immutable, append-only receipt.
This service verifies that contract is honored end-to-end.

Usage:
  reconciler = ReceiptReconciler(supabase_client)
  report = await reconciler.reconcile(since=datetime(...))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.receipt_store import get_receipt_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationGap:
    """A receipt present in one store but missing from the other."""

    receipt_id: str
    correlation_id: str
    action_type: str
    source: str  # "memory_only" | "supabase_only"
    created_at: str
    suite_id: str


@dataclass
class ReconciliationReport:
    """Result of a reconciliation run."""

    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str = ""
    memory_count: int = 0
    supabase_count: int = 0
    matched: int = 0
    gaps: list[ReconciliationGap] = field(default_factory=list)
    status: str = "pending"  # "pending" | "clean" | "gaps_found" | "error"
    error: str | None = None

    @property
    def gap_count(self) -> int:
        return len(self.gaps)

    @property
    def memory_only_count(self) -> int:
        return sum(1 for g in self.gaps if g.source == "memory_only")

    @property
    def supabase_only_count(self) -> int:
        return sum(1 for g in self.gaps if g.source == "supabase_only")

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "memory_count": self.memory_count,
            "supabase_count": self.supabase_count,
            "matched": self.matched,
            "gap_count": self.gap_count,
            "memory_only": self.memory_only_count,
            "supabase_only": self.supabase_only_count,
            "status": self.status,
            "error": self.error,
            "gaps": [
                {
                    "receipt_id": g.receipt_id,
                    "correlation_id": g.correlation_id,
                    "action_type": g.action_type,
                    "source": g.source,
                    "created_at": g.created_at,
                    "suite_id": g.suite_id,
                }
                for g in self.gaps
            ],
        }


class ReceiptReconciler:
    """Compare in-memory receipt store with Supabase for consistency."""

    def __init__(self, supabase_client: Any = None) -> None:
        self._supabase = supabase_client
        self._store = get_receipt_store()

    async def reconcile(
        self,
        since: datetime | None = None,
        suite_id: str | None = None,
    ) -> ReconciliationReport:
        """Run a reconciliation pass.

        Args:
            since: Only check receipts created after this timestamp.
            suite_id: Optional filter to a single suite.

        Returns:
            ReconciliationReport with gaps (if any).
        """
        report = ReconciliationReport()

        try:
            # 1. Get all in-memory receipts
            memory_receipts = self._store.get_all()
            if suite_id:
                memory_receipts = [
                    r for r in memory_receipts
                    if r.get("suite_id") == suite_id
                ]
            if since:
                since_iso = since.isoformat()
                memory_receipts = [
                    r for r in memory_receipts
                    if r.get("created_at", "") >= since_iso
                ]
            report.memory_count = len(memory_receipts)

            memory_ids = {r.get("id", r.get("receipt_id", "")) for r in memory_receipts}
            memory_by_id = {
                r.get("id", r.get("receipt_id", "")): r for r in memory_receipts
            }

            # 2. Get Supabase receipts (if client available)
            supabase_ids: set[str] = set()
            supabase_by_id: dict[str, dict[str, Any]] = {}

            if self._supabase:
                supabase_receipts = await self._fetch_supabase_receipts(
                    since=since, suite_id=suite_id
                )
                report.supabase_count = len(supabase_receipts)
                supabase_ids = {
                    r.get("id", r.get("receipt_id", ""))
                    for r in supabase_receipts
                }
                supabase_by_id = {
                    r.get("id", r.get("receipt_id", "")): r
                    for r in supabase_receipts
                }
            else:
                logger.info("Reconciliation: Supabase client not configured, memory-only check")
                report.supabase_count = 0

            # 3. Find gaps
            # In memory but not in Supabase
            memory_only = memory_ids - supabase_ids
            for rid in memory_only:
                r = memory_by_id.get(rid, {})
                report.gaps.append(ReconciliationGap(
                    receipt_id=rid,
                    correlation_id=r.get("correlation_id", ""),
                    action_type=r.get("action_type", ""),
                    source="memory_only",
                    created_at=r.get("created_at", ""),
                    suite_id=r.get("suite_id", ""),
                ))

            # In Supabase but not in memory (could be from previous process)
            supabase_only = supabase_ids - memory_ids
            for rid in supabase_only:
                r = supabase_by_id.get(rid, {})
                report.gaps.append(ReconciliationGap(
                    receipt_id=rid,
                    correlation_id=r.get("correlation_id", ""),
                    action_type=r.get("action_type", ""),
                    source="supabase_only",
                    created_at=r.get("created_at", ""),
                    suite_id=r.get("suite_id", ""),
                ))

            # 4. Count matched
            report.matched = len(memory_ids & supabase_ids)

            # 5. Set status
            if report.gap_count == 0:
                report.status = "clean"
            else:
                report.status = "gaps_found"
                logger.warning(
                    "Reconciliation found %d gaps (%d memory-only, %d supabase-only)",
                    report.gap_count,
                    report.memory_only_count,
                    report.supabase_only_count,
                )

        except Exception as e:
            report.status = "error"
            report.error = str(e)[:500]
            logger.error("Reconciliation failed: %s", e)

        report.completed_at = datetime.now(timezone.utc).isoformat()
        return report

    async def _fetch_supabase_receipts(
        self,
        since: datetime | None = None,
        suite_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch receipts from Supabase for comparison."""
        try:
            query = self._supabase.table("receipts").select("*")
            if since:
                query = query.gte("created_at", since.isoformat())
            if suite_id:
                query = query.eq("suite_id", suite_id)
            query = query.order("created_at", desc=False)
            result = query.execute()
            return result.data if result.data else []
        except Exception as e:
            logger.error("Failed to fetch Supabase receipts: %s", e)
            return []


# Module-level singleton
_reconciler: ReceiptReconciler | None = None


def get_reconciler(supabase_client: Any = None) -> ReceiptReconciler:
    """Get or create the receipt reconciler singleton."""
    global _reconciler
    if _reconciler is None:
        _reconciler = ReceiptReconciler(supabase_client)
    return _reconciler
