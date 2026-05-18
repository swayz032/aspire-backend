"""Brief Materializer — refresh office / finance / service / thread brief caches.

Each public method reads recent memory + open candidates + pending approvals
+ recent receipts, builds a JSON projection of the brief, and UPSERTs the
result into the appropriate *_brief_cache table (migration 098 / 101). The
freshness_seq column is monotonically incremented on every refresh so
readers can detect concurrent rebuilds.

Refresh policy:
  - cache hit (last_built_at > now() - 60s) AND refresh=False -> return cache
  - otherwise: recompute, UPSERT, return new row.

Visibility scope (Law #6):
  - build_office_brief filters memory by visibility_scope='office'
  - build_finance_brief filters memory by visibility_scope='finance'
  - build_service_brief filters memory by visibility_scope='service'
  - build_thread_brief reads only memory that already exists in the thread
    (visibility scope already enforced by the writer at memory_objects layer)

Receipts (Law #2):
  - build_office_brief / build_finance_brief / build_thread_brief do NOT emit
    receipts directly — the cache is a derivation of source-of-truth tables
    that each already have receipts. Re-emitting on every refresh would
    multiply receipt volume by ~3x without adding audit value.
  - build_service_brief DOES emit a receipt (action=memory.service_brief.built)
    because the service brief aggregates across multiple actor domains (drew,
    adam, dispatch) and the Law #2 gap was identified in the office-memory
    review notes. Service brief receipts close that audit gap for the
    highest-traffic new domain.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import uuid

from aspire_orchestrator.schemas.memory_v1 import (
    FinanceBriefOut,
    OfficeBriefOut,
    ScopedIdentity,
    ServiceBriefOut,
    ThreadBriefOut,
)
from aspire_orchestrator.services.memory_service import (
    MemoryServiceError,
    _assert_scope_match,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_upsert,
)

logger = logging.getLogger(__name__)


# 60-second freshness window — matches the Temporal sweep cadence in the plan.
_FRESHNESS_SECONDS = 60


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_dt(value: Any) -> datetime | None:
    """Coerce DB-string or datetime to timezone-aware datetime; None passthrough."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        # Supabase returns ISO8601 like '2026-04-29T12:34:56.789012+00:00'
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _office_row_to_out(row: dict[str, Any]) -> OfficeBriefOut:
    return OfficeBriefOut(
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
        brief_text=row.get("brief_text"),
        brief_json=row.get("brief_json") or {},
        due_now_count=int(row.get("due_now_count", 0)),
        overdue_count=int(row.get("overdue_count", 0)),
        pending_approval_count=int(row.get("pending_approval_count", 0)),
        recent_receipts_count=int(row.get("recent_receipts_count", 0)),
        last_built_at=_to_dt(row["last_built_at"]) or _now_utc(),
        freshness_seq=int(row.get("freshness_seq", 0)),
    )


def _finance_row_to_out(row: dict[str, Any]) -> FinanceBriefOut:
    return FinanceBriefOut(
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
        brief_text=row.get("brief_text"),
        brief_json=row.get("brief_json") or {},
        due_now_count=int(row.get("due_now_count", 0)),
        overdue_count=int(row.get("overdue_count", 0)),
        pending_approval_count=int(row.get("pending_approval_count", 0)),
        recent_receipts_count=int(row.get("recent_receipts_count", 0)),
        provider_health=row.get("provider_health") or {},
        aging_summary=row.get("aging_summary") or {},
        cash_narrative=row.get("cash_narrative"),
        last_built_at=_to_dt(row["last_built_at"]) or _now_utc(),
        freshness_seq=int(row.get("freshness_seq", 0)),
    )


def _service_row_to_out(row: dict[str, Any]) -> ServiceBriefOut:
    brief_json = row.get("brief_json") or {}
    return ServiceBriefOut(
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
        brief_text=row.get("brief_text"),
        brief_json=brief_json,
        due_now_count=int(row.get("due_now_count", 0)),
        overdue_count=int(row.get("overdue_count", 0)),
        pending_approval_count=int(row.get("pending_approval_count", 0)),
        recent_receipts_count=int(row.get("recent_receipts_count", 0)),
        # Service-specific counters — populated from brief_json on build; 0 on cache replay
        recent_picks_count=int(brief_json.get("recent_picks_count", 0)),
        recent_overrides_count=int(brief_json.get("recent_overrides_count", 0)),
        open_pending_intents_count=int(brief_json.get("open_pending_intents_count", 0)),
        recent_handoffs_count=int(brief_json.get("recent_handoffs_count", 0)),
        active_threads_count=int(brief_json.get("active_threads_count", 0)),
        last_built_at=_to_dt(row["last_built_at"]) or _now_utc(),
        freshness_seq=int(row.get("freshness_seq", 0)),
    )


def _thread_row_to_out(row: dict[str, Any]) -> ThreadBriefOut:
    return ThreadBriefOut(
        thread_id=UUID(row["thread_id"]),
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        summary=row.get("summary"),
        last_promise=row.get("last_promise"),
        pending_blockers=row.get("pending_blockers") or [],
        latest_receipt_id=row.get("latest_receipt_id"),
        next_best_action=row.get("next_best_action") or {},
        last_built_at=_to_dt(row["last_built_at"]) or _now_utc(),
        freshness_seq=int(row.get("freshness_seq", 0)),
    )


class BriefMaterializer:
    """Build and cache office / finance / thread briefs.

    Stateless. Reads source-of-truth tables and UPSERTs into *_brief_cache.
    """

    # ---------------------------------------------------------------------------
    # Office brief
    # ---------------------------------------------------------------------------

    async def build_office_brief(
        self,
        office_id: UUID,
        *,
        scope: ScopedIdentity,
        refresh: bool = False,
    ) -> OfficeBriefOut:
        """Return the office brief for (scope.tenant, scope.suite, office_id).

        If refresh=False and the cache is < 60s old, returns the cache.
        Otherwise rebuilds + UPSERTs. freshness_seq monotonically increases.
        """
        if str(office_id) != str(scope.office_id):
            raise MemoryServiceError(
                f"office_id={office_id} does not match scope.office_id={scope.office_id}",
                code="TENANT_ISOLATION_VIOLATION",
                tenant_id=scope.tenant_id,
            )

        cached = await self._fetch_office_cache(scope)
        if cached and not refresh and self._is_fresh(cached.last_built_at):
            return cached

        # Recompute
        recent_memory = await self._fetch_recent_memory(
            scope=scope, visibility_scope="office", limit=20
        )
        open_candidates = await self._fetch_open_candidates(scope=scope, limit=20)
        pending_approvals = await self._fetch_pending_approvals(scope=scope, limit=20)
        recent_receipts = await self._fetch_recent_receipts(scope=scope, limit=20)

        now = _now_utc()
        due_now_count = self._count_due_now(open_candidates, now=now)
        overdue_count = self._count_overdue(open_candidates, now=now)

        brief_json: dict[str, Any] = {
            "recent_memory": [self._project_memory(m) for m in recent_memory],
            "open_candidates": [self._project_candidate(c) for c in open_candidates],
            "pending_approvals": [self._project_approval(a) for a in pending_approvals],
            "recent_receipts": [self._project_receipt(r) for r in recent_receipts],
        }
        brief_text = self._render_office_text(
            recent_memory_count=len(recent_memory),
            open_candidate_count=len(open_candidates),
            pending_approval_count=len(pending_approvals),
            due_now_count=due_now_count,
            overdue_count=overdue_count,
        )

        next_seq = (cached.freshness_seq + 1) if cached else 1
        upsert_row = {
            "tenant_id": str(scope.tenant_id),
            "suite_id": str(scope.suite_id),
            "office_id": str(office_id),
            "brief_text": brief_text,
            "brief_json": brief_json,
            "due_now_count": due_now_count,
            "overdue_count": overdue_count,
            "pending_approval_count": len(pending_approvals),
            "recent_receipts_count": len(recent_receipts),
            "last_built_at": now.isoformat(),
            "freshness_seq": next_seq,
        }
        try:
            row = await supabase_upsert(
                "office_brief_cache",
                upsert_row,
                on_conflict="tenant_id,suite_id,office_id",
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB upsert office_brief_cache failed: {exc.detail}",
                code="DB_UPSERT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        out = _office_row_to_out(row)
        logger.info(
            "brief_materializer: office brief built tenant=%s office=%s seq=%d",
            str(scope.tenant_id),
            str(office_id),
            out.freshness_seq,
        )
        return out

    # ---------------------------------------------------------------------------
    # Finance brief
    # ---------------------------------------------------------------------------

    async def build_finance_brief(
        self,
        office_id: UUID,
        *,
        scope: ScopedIdentity,
        refresh: bool = False,
    ) -> FinanceBriefOut:
        """Return the finance brief. Same shape as office brief but
        visibility_scope='finance' + 3 extra columns."""
        if str(office_id) != str(scope.office_id):
            raise MemoryServiceError(
                f"office_id={office_id} does not match scope.office_id={scope.office_id}",
                code="TENANT_ISOLATION_VIOLATION",
                tenant_id=scope.tenant_id,
            )

        cached = await self._fetch_finance_cache(scope)
        if cached and not refresh and self._is_fresh(cached.last_built_at):
            return cached

        recent_memory = await self._fetch_recent_memory(
            scope=scope, visibility_scope="finance", limit=20
        )
        open_candidates = await self._fetch_open_candidates(scope=scope, limit=20)
        pending_approvals = await self._fetch_pending_approvals(scope=scope, limit=20)
        recent_receipts = await self._fetch_recent_receipts(scope=scope, limit=20)
        provider_health = await self._fetch_provider_health(scope=scope)

        now = _now_utc()
        due_now_count = self._count_due_now(open_candidates, now=now)
        overdue_count = self._count_overdue(open_candidates, now=now)

        brief_json: dict[str, Any] = {
            "recent_memory": [self._project_memory(m) for m in recent_memory],
            "open_candidates": [self._project_candidate(c) for c in open_candidates],
            "pending_approvals": [self._project_approval(a) for a in pending_approvals],
            "recent_receipts": [self._project_receipt(r) for r in recent_receipts],
        }
        brief_text = self._render_finance_text(
            recent_memory_count=len(recent_memory),
            open_candidate_count=len(open_candidates),
            pending_approval_count=len(pending_approvals),
            due_now_count=due_now_count,
            overdue_count=overdue_count,
        )

        next_seq = (cached.freshness_seq + 1) if cached else 1
        # V1: aging_summary stub. Future passes hook into the finance retrieval pipeline.
        aging_summary: dict[str, Any] = {}
        cash_narrative: str | None = None

        upsert_row = {
            "tenant_id": str(scope.tenant_id),
            "suite_id": str(scope.suite_id),
            "office_id": str(office_id),
            "brief_text": brief_text,
            "brief_json": brief_json,
            "due_now_count": due_now_count,
            "overdue_count": overdue_count,
            "pending_approval_count": len(pending_approvals),
            "recent_receipts_count": len(recent_receipts),
            "provider_health": provider_health,
            "aging_summary": aging_summary,
            "cash_narrative": cash_narrative,
            "last_built_at": now.isoformat(),
            "freshness_seq": next_seq,
        }
        try:
            row = await supabase_upsert(
                "finance_brief_cache",
                upsert_row,
                on_conflict="tenant_id,suite_id,office_id",
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB upsert finance_brief_cache failed: {exc.detail}",
                code="DB_UPSERT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        out = _finance_row_to_out(row)
        logger.info(
            "brief_materializer: finance brief built tenant=%s office=%s seq=%d",
            str(scope.tenant_id),
            str(office_id),
            out.freshness_seq,
        )
        return out

    # ---------------------------------------------------------------------------
    # Service brief (Wave 5.1b-4)
    # ---------------------------------------------------------------------------

    async def build_service_brief(
        self,
        office_id: UUID,
        *,
        scope: ScopedIdentity,
        refresh: bool = False,
    ) -> ServiceBriefOut:
        """Build/refresh service_brief_cache for the given office.

        Reads recent service-scope memory_objects (picks, overrides, pending
        intents, handoff notes) + service-domain open candidates + pending
        approvals + recent receipts. UPSERTs into service_brief_cache.

        Law #2: Emits memory.service_brief.built receipt on every build/refresh.
        Law #6: All queries scoped by tenant_id + suite_id + office_id.
        Law #9: Only counts and IDs are logged; no content or PII.

        Args:
            office_id: Must match scope.office_id (Law #6 — fail closed).
            scope:     ScopedIdentity carrying tenant_id / suite_id / office_id.
            refresh:   If True, bypass the 60s TTL and force a rebuild.

        Returns:
            ServiceBriefOut — the freshly built (or cached) brief.

        Raises:
            MemoryServiceError: office_id / scope mismatch or DB upsert failure.
        """
        if str(office_id) != str(scope.office_id):
            raise MemoryServiceError(
                f"office_id={office_id} does not match scope.office_id={scope.office_id}",
                code="TENANT_ISOLATION_VIOLATION",
                tenant_id=scope.tenant_id,
            )

        cached = await self._fetch_service_cache(scope)
        if cached and not refresh and self._is_fresh(cached.last_built_at):
            return cached

        # ------------------------------------------------------------------ #
        # Aggregate source data                                                #
        # ------------------------------------------------------------------ #
        recent_picks = await self._fetch_service_decision_facts(
            scope=scope, decision_type="material_pick", limit=5
        )
        recent_overrides = await self._fetch_service_decision_facts(
            scope=scope, decision_type="material_override", limit=3
        )
        open_pending_intents = await self._fetch_service_pending_intents(
            scope=scope, limit=20
        )
        recent_handoffs = await self._fetch_service_handoffs(scope=scope, limit=3)
        active_threads_count = await self._fetch_service_active_threads_count(scope=scope)
        open_candidates = await self._fetch_open_candidates(scope=scope, limit=20)
        pending_approvals = await self._fetch_pending_approvals(scope=scope, limit=20)
        recent_receipts = await self._fetch_recent_receipts(scope=scope, limit=10)

        now = _now_utc()
        due_now_count = self._count_due_now(open_candidates, now=now)
        overdue_count = self._count_overdue(open_candidates, now=now)

        # ------------------------------------------------------------------ #
        # Build brief_json — counts embedded for cache replay                 #
        # ------------------------------------------------------------------ #
        brief_json: dict[str, Any] = {
            "recent_picks": [self._project_memory(m) for m in recent_picks],
            "recent_overrides": [self._project_memory(m) for m in recent_overrides],
            "open_pending_intents": [self._project_memory(m) for m in open_pending_intents],
            "recent_handoffs": [self._project_memory(m) for m in recent_handoffs],
            "open_candidates": [self._project_candidate(c) for c in open_candidates],
            "pending_approvals": [self._project_approval(a) for a in pending_approvals],
            "recent_receipts": [self._project_receipt(r) for r in recent_receipts],
            # Embed counts into brief_json so _service_row_to_out() can recover
            # them on cache replay without re-querying source tables.
            "recent_picks_count": len(recent_picks),
            "recent_overrides_count": len(recent_overrides),
            "open_pending_intents_count": len(open_pending_intents),
            "recent_handoffs_count": len(recent_handoffs),
            "active_threads_count": active_threads_count,
        }
        brief_text = self._render_service_text(
            recent_picks_count=len(recent_picks),
            recent_overrides_count=len(recent_overrides),
            open_pending_intents_count=len(open_pending_intents),
            recent_handoffs_count=len(recent_handoffs),
            active_threads_count=active_threads_count,
            open_candidate_count=len(open_candidates),
            pending_approval_count=len(pending_approvals),
            due_now_count=due_now_count,
            overdue_count=overdue_count,
        )

        next_seq = (cached.freshness_seq + 1) if cached else 1
        upsert_row = {
            "tenant_id": str(scope.tenant_id),
            "suite_id": str(scope.suite_id),
            "office_id": str(office_id),
            "brief_text": brief_text,
            "brief_json": brief_json,
            "due_now_count": due_now_count,
            "overdue_count": overdue_count,
            "pending_approval_count": len(pending_approvals),
            "recent_receipts_count": len(recent_receipts),
            "last_built_at": now.isoformat(),
            "freshness_seq": next_seq,
        }

        # ------------------------------------------------------------------ #
        # Upsert                                                               #
        # ------------------------------------------------------------------ #
        correlation_id = str(uuid.uuid4())
        success = False
        try:
            row = await supabase_upsert(
                "service_brief_cache",
                upsert_row,
                on_conflict="tenant_id,suite_id,office_id",
            )
            success = True
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB upsert service_brief_cache failed: {exc.detail}",
                code="DB_UPSERT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc
        finally:
            # ---------------------------------------------------------------- #
            # Law #2 — emit receipt for every service brief build attempt       #
            # (success OR failure). Counts only — no PII, no content (Law #9). #
            # ---------------------------------------------------------------- #
            receipt_outcome = "ok" if success else "failed"
            try:
                from aspire_orchestrator.services.receipt_store import store_receipts
                store_receipts([{
                    "id": str(uuid.uuid4()),
                    "correlation_id": correlation_id,
                    "receipt_type": "memory.service_brief.built",
                    "action_type": "memory.service_brief.built",
                    "actor": "system",
                    "actor_type": "SYSTEM",
                    "risk_tier": "GREEN",
                    "suite_id": str(scope.suite_id),
                    "tenant_id": str(scope.tenant_id),
                    "office_id": str(office_id),
                    "outcome": receipt_outcome,
                    "details": {
                        "picks_count": len(recent_picks),
                        "overrides_count": len(recent_overrides),
                        "open_pending_intents_count": len(open_pending_intents),
                        "recent_handoffs_count": len(recent_handoffs),
                        "active_threads_count": active_threads_count,
                        "open_candidates_count": len(open_candidates),
                        "pending_approvals_count": len(pending_approvals),
                        "recent_receipts_count": len(recent_receipts),
                        "freshness_seq": next_seq,
                    },
                }])
            except Exception as receipt_exc:
                # Never let receipt emission crash the pipeline (but do log it)
                logger.warning(
                    "brief_materializer: service brief receipt emit failed: %s",
                    receipt_exc,
                )

        out = _service_row_to_out(row)
        # Law #9: log counts only, never the brief content
        logger.info(
            "brief_materializer: service brief built tenant=%s office=%s seq=%d "
            "picks=%d overrides=%d pending_intents=%d handoffs=%d threads=%d",
            str(scope.tenant_id),
            str(office_id),
            out.freshness_seq,
            len(recent_picks),
            len(recent_overrides),
            len(open_pending_intents),
            len(recent_handoffs),
            active_threads_count,
        )
        return out

    # ---------------------------------------------------------------------------
    # Thread brief
    # ---------------------------------------------------------------------------

    async def build_thread_brief(
        self,
        thread_id: UUID,
        *,
        scope: ScopedIdentity,
        refresh: bool = False,
    ) -> ThreadBriefOut:
        """Return the thread brief for thread_id.

        Pulls from memory_objects (last_promise from latest pending_intent;
        next_best_action from latest non-rejected memory's summary), open
        candidates linked to this thread, latest receipt linked.
        """
        cached = await self._fetch_thread_cache(thread_id, scope)
        if cached and not refresh and self._is_fresh(cached.last_built_at):
            return cached

        # Validate the thread belongs to this tenant before reading
        thread_row = await self._fetch_thread_row(thread_id, scope)
        if thread_row is None:
            raise MemoryServiceError(
                f"thread_id={thread_id} not found in tenant={scope.tenant_id}",
                code="NOT_FOUND",
                tenant_id=scope.tenant_id,
            )

        # Pull recent memory in this thread
        thread_memory = await self._fetch_thread_memory(
            thread_id=thread_id, scope=scope, limit=20
        )
        thread_candidates = await self._fetch_thread_candidates(
            thread_id=thread_id, scope=scope, limit=20
        )

        # last_promise: latest pending_intent.summary
        last_promise = self._extract_last_promise(thread_memory)
        # next_best_action: latest non-rejected memory's summary (V1)
        next_best_action = self._extract_next_best_action(thread_memory)
        # summary: stitched paragraph
        summary = self._render_thread_summary(thread_memory)

        # latest_receipt_id: from threads.latest_receipt_id (already maintained)
        latest_receipt_id = thread_row.get("latest_receipt_id")

        # pending_blockers: project candidates
        pending_blockers = [
            self._project_candidate(c) for c in thread_candidates
        ]

        next_seq = (cached.freshness_seq + 1) if cached else 1
        now = _now_utc()
        upsert_row = {
            "thread_id": str(thread_id),
            "tenant_id": str(scope.tenant_id),
            "suite_id": str(scope.suite_id),
            "summary": summary,
            "last_promise": last_promise,
            "pending_blockers": pending_blockers,
            "latest_receipt_id": latest_receipt_id,
            "next_best_action": next_best_action,
            "last_built_at": now.isoformat(),
            "freshness_seq": next_seq,
        }
        try:
            row = await supabase_upsert(
                "thread_brief_cache",
                upsert_row,
                on_conflict="thread_id",
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB upsert thread_brief_cache failed: {exc.detail}",
                code="DB_UPSERT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        out = _thread_row_to_out(row)
        logger.info(
            "brief_materializer: thread brief built tenant=%s thread=%s seq=%d",
            str(scope.tenant_id),
            str(thread_id),
            out.freshness_seq,
        )
        return out

    # ---------------------------------------------------------------------------
    # Cache fetch helpers
    # ---------------------------------------------------------------------------

    async def _fetch_office_cache(
        self, scope: ScopedIdentity
    ) -> OfficeBriefOut | None:
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
        )
        try:
            rows = await supabase_select("office_brief_cache", filter_str, limit=1)
        except SupabaseClientError:
            return None
        return _office_row_to_out(rows[0]) if rows else None

    async def _fetch_finance_cache(
        self, scope: ScopedIdentity
    ) -> FinanceBriefOut | None:
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
        )
        try:
            rows = await supabase_select("finance_brief_cache", filter_str, limit=1)
        except SupabaseClientError:
            return None
        return _finance_row_to_out(rows[0]) if rows else None

    async def _fetch_service_cache(
        self, scope: ScopedIdentity
    ) -> ServiceBriefOut | None:
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
        )
        try:
            rows = await supabase_select("service_brief_cache", filter_str, limit=1)
        except SupabaseClientError:
            return None
        return _service_row_to_out(rows[0]) if rows else None

    async def _fetch_thread_cache(
        self, thread_id: UUID, scope: ScopedIdentity
    ) -> ThreadBriefOut | None:
        filter_str = (
            f"thread_id=eq.{thread_id}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
        )
        try:
            rows = await supabase_select("thread_brief_cache", filter_str, limit=1)
        except SupabaseClientError:
            return None
        if not rows:
            return None
        return _thread_row_to_out(rows[0])

    # ---------------------------------------------------------------------------
    # Source-of-truth fetch helpers
    # ---------------------------------------------------------------------------

    async def _fetch_recent_memory(
        self,
        *,
        scope: ScopedIdentity,
        visibility_scope: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch recent non-rejected, non-superseded memory in the scope."""
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
            f"&visibility_scope=eq.{visibility_scope}"
            f"&status=not.in.(rejected,superseded)"
        )
        try:
            return await supabase_select(
                "memory_objects",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit,
            )
        except SupabaseClientError as exc:
            logger.warning(
                "brief_materializer: memory fetch failed scope=%s vis=%s: %s",
                scope.tenant_id,
                visibility_scope,
                exc.detail,
            )
            return []

    async def _fetch_open_candidates(
        self,
        *,
        scope: ScopedIdentity,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch open + snoozed candidates in scope."""
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
            f"&status=in.(open,snoozed)"
        )
        try:
            return await supabase_select(
                "proactive_candidates",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit,
            )
        except SupabaseClientError:
            return []

    async def _fetch_pending_approvals(
        self,
        *,
        scope: ScopedIdentity,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch pending approval_links in scope (suite-level — approvals are suite-scoped)."""
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&approval_status=eq.pending"
        )
        try:
            return await supabase_select(
                "approval_links",
                filter_str,
                order_by="created_at.desc",
                limit=limit,
            )
        except SupabaseClientError:
            return []

    async def _fetch_recent_receipts(
        self,
        *,
        scope: ScopedIdentity,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch recent receipts via in-memory store_receipts query.

        receipts table is suite-scoped via RLS. We delegate to receipt_store
        for the canonical list. Failure is degraded to empty.
        """
        try:
            from aspire_orchestrator.services.receipt_store import query_receipts
            return query_receipts(suite_id=str(scope.suite_id), limit=limit)
        except Exception as exc:
            logger.debug(
                "brief_materializer: receipts query failed: %s", exc
            )
            return []

    async def _fetch_provider_health(
        self, *, scope: ScopedIdentity
    ) -> dict[str, Any]:
        """Pull a snapshot of provider health for the finance brief.

        V1: best-effort hook into the provider_call_logger if available;
        otherwise return an empty dict. Future passes can wire this to the
        actual provider health surface.
        """
        try:
            from aspire_orchestrator.services.provider_call_logger import (
                get_provider_call_logger,
            )
            logger_inst = get_provider_call_logger()
            # provider_call_logger may not expose a direct health snapshot.
            # V1: return an empty map; the column is JSONB and tolerates {}.
            if hasattr(logger_inst, "snapshot_health"):
                snapshot = getattr(logger_inst, "snapshot_health")
                if callable(snapshot):
                    return dict(snapshot()) or {}
        except Exception:
            pass
        return {}

    async def _fetch_service_decision_facts(
        self,
        *,
        scope: ScopedIdentity,
        decision_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch recent decision_fact memory objects filtered by a detail.decision_type tag.

        decision_type is stored inside the detail JSONB column:
          { "decision_type": "material_pick" | "material_override", ... }

        PostgREST JSONB path filter: detail->>'decision_type'=eq.{decision_type}
        maps to the PostgREST operator `detail->>decision_type=eq.{value}`.
        """
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
            f"&visibility_scope=eq.service"
            f"&memory_type=eq.decision_fact"
            f"&status=not.in.(rejected,superseded)"
        )
        try:
            rows = await supabase_select(
                "memory_objects",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit * 5,  # over-fetch then filter client-side (PostgREST JSONB filter workaround)
            )
        except SupabaseClientError as exc:
            logger.warning(
                "brief_materializer: decision_fact fetch failed type=%s scope=%s: %s",
                decision_type,
                scope.tenant_id,
                exc.detail,
            )
            return []

        # Client-side filter on detail.decision_type (avoids complex PostgREST encoding)
        filtered = [
            r for r in rows
            if isinstance(r.get("detail"), dict)
            and r["detail"].get("decision_type") == decision_type
        ]
        return filtered[:limit]

    async def _fetch_service_pending_intents(
        self,
        *,
        scope: ScopedIdentity,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch unresolved pending_intent memory objects in service scope."""
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
            f"&visibility_scope=eq.service"
            f"&memory_type=eq.pending_intent"
            f"&status=not.in.(executed,rejected,superseded)"
        )
        try:
            return await supabase_select(
                "memory_objects",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit,
            )
        except SupabaseClientError as exc:
            logger.warning(
                "brief_materializer: pending_intent fetch failed scope=%s: %s",
                scope.tenant_id,
                exc.detail,
            )
            return []

    async def _fetch_service_handoffs(
        self,
        *,
        scope: ScopedIdentity,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch recent handoff_note memory objects with visibility_scope='service'."""
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
            f"&visibility_scope=eq.service"
            f"&memory_type=eq.handoff_note"
            f"&status=not.in.(rejected,superseded)"
        )
        try:
            return await supabase_select(
                "memory_objects",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit,
            )
        except SupabaseClientError as exc:
            logger.warning(
                "brief_materializer: handoff_note fetch failed scope=%s: %s",
                scope.tenant_id,
                exc.detail,
            )
            return []

    async def _fetch_service_active_threads_count(
        self,
        *,
        scope: ScopedIdentity,
    ) -> int:
        """Count open project_thread / job_thread / property_thread with recent activity.

        'Recent' = last_activity_at within 7 days. Returns 0 on any error
        (best-effort counter — the brief is still valid without this).
        """
        cutoff = (_now_utc() - timedelta(days=7)).isoformat()
        filter_str = (
            f"tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
            f"&thread_type=in.(project_thread,job_thread,property_thread)"
            f"&status=eq.open"
            f"&last_activity_at=gte.{cutoff}"
        )
        try:
            rows = await supabase_select(
                "threads",
                filter_str,
                order_by="last_activity_at.desc",
                limit=500,  # practical cap; real COUNT(*) would require RPC
            )
            return len(rows)
        except SupabaseClientError as exc:
            logger.warning(
                "brief_materializer: active_threads count failed scope=%s: %s",
                scope.tenant_id,
                exc.detail,
            )
            return 0

    async def _fetch_thread_row(
        self,
        thread_id: UUID,
        scope: ScopedIdentity,
    ) -> dict[str, Any] | None:
        """Fetch the thread row + scope-validate."""
        filter_str = (
            f"thread_id=eq.{thread_id}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
        )
        try:
            rows = await supabase_select("threads", filter_str, limit=1)
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB select threads failed: {exc.detail}",
                code="DB_SELECT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc
        if not rows:
            return None
        # Defense-in-depth scope assertion
        _assert_scope_match(rows[0], scope)
        return rows[0]

    async def _fetch_thread_memory(
        self,
        *,
        thread_id: UUID,
        scope: ScopedIdentity,
        limit: int,
    ) -> list[dict[str, Any]]:
        filter_str = (
            f"thread_id=eq.{thread_id}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
            f"&status=not.in.(rejected,superseded)"
        )
        try:
            return await supabase_select(
                "memory_objects",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit,
            )
        except SupabaseClientError:
            return []

    async def _fetch_thread_candidates(
        self,
        *,
        thread_id: UUID,
        scope: ScopedIdentity,
        limit: int,
    ) -> list[dict[str, Any]]:
        filter_str = (
            f"thread_id=eq.{thread_id}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
            f"&status=in.(open,snoozed)"
        )
        try:
            return await supabase_select(
                "proactive_candidates",
                filter_str,
                order_by="last_activity_at.desc",
                limit=limit,
            )
        except SupabaseClientError:
            return []

    # ---------------------------------------------------------------------------
    # Projection helpers — keep these small + PII-free
    # ---------------------------------------------------------------------------

    @staticmethod
    def _project_memory(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "memory_id": row.get("memory_id"),
            "memory_type": row.get("memory_type"),
            "title": row.get("title"),
            "summary": row.get("summary"),
            "thread_id": row.get("thread_id"),
            "last_activity_at": row.get("last_activity_at"),
            "status": row.get("status"),
        }

    @staticmethod
    def _project_candidate(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": row.get("candidate_id"),
            "owner_agent": row.get("owner_agent"),
            "recommended_action": row.get("recommended_action"),
            "action_class": row.get("action_class"),
            "why_now": row.get("why_now"),
            "risk_tier": row.get("risk_tier"),
            "needs_approval": row.get("needs_approval"),
            "due_at": row.get("due_at"),
            "status": row.get("status"),
        }

    @staticmethod
    def _project_approval(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "approval_id": row.get("approval_id"),
            "requested_by_agent": row.get("requested_by_agent"),
            "approval_status": row.get("approval_status"),
            "requested_at": row.get("requested_at"),
            "linked_candidate_id": row.get("linked_candidate_id"),
        }

    @staticmethod
    def _project_receipt(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "receipt_id": row.get("id") or row.get("receipt_id"),
            "receipt_type": row.get("receipt_type"),
            "outcome": row.get("outcome") or row.get("status"),
            "created_at": row.get("created_at"),
        }

    # ---------------------------------------------------------------------------
    # Counters + extractors
    # ---------------------------------------------------------------------------

    @staticmethod
    def _count_due_now(
        candidates: list[dict[str, Any]], *, now: datetime
    ) -> int:
        """Count candidates with due_at <= now + 1h."""
        deadline = now + timedelta(hours=1)
        n = 0
        for c in candidates:
            due_at = _to_dt(c.get("due_at"))
            if due_at is None:
                continue
            if due_at <= deadline:
                n += 1
        return n

    @staticmethod
    def _count_overdue(
        candidates: list[dict[str, Any]], *, now: datetime
    ) -> int:
        """Count candidates with due_at < now (already overdue)."""
        n = 0
        for c in candidates:
            due_at = _to_dt(c.get("due_at"))
            if due_at is None:
                continue
            if due_at < now:
                n += 1
        return n

    @staticmethod
    def _extract_last_promise(thread_memory: list[dict[str, Any]]) -> str | None:
        """Return summary of the latest pending_intent in the thread, if any."""
        for m in thread_memory:
            if m.get("memory_type") == "pending_intent":
                return m.get("summary")
        return None

    @staticmethod
    def _extract_next_best_action(
        thread_memory: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return {'text': latest non-rejected memory summary} or {}."""
        if not thread_memory:
            return {}
        # thread_memory is already filtered (status NOT IN rejected/superseded)
        # ordered last_activity_at DESC.
        latest = thread_memory[0]
        text = latest.get("summary")
        if not text:
            return {}
        return {
            "text": text,
            "memory_id": latest.get("memory_id"),
            "memory_type": latest.get("memory_type"),
        }

    @staticmethod
    def _render_thread_summary(thread_memory: list[dict[str, Any]]) -> str | None:
        """V1 thread summary: stitch the three most recent memory summaries.

        Future passes can drop in an LLM-driven narrative.
        """
        if not thread_memory:
            return None
        parts: list[str] = []
        for m in thread_memory[:3]:
            s = m.get("summary")
            if not s:
                continue
            mt = m.get("memory_type", "memory")
            parts.append(f"[{mt}] {s}")
        if not parts:
            return None
        return "\n".join(parts)

    # ---------------------------------------------------------------------------
    # Brief text rendering — terse, deterministic, PII-free
    # ---------------------------------------------------------------------------

    @staticmethod
    def _render_office_text(
        *,
        recent_memory_count: int,
        open_candidate_count: int,
        pending_approval_count: int,
        due_now_count: int,
        overdue_count: int,
    ) -> str:
        return (
            f"Office brief: {recent_memory_count} recent memory items, "
            f"{open_candidate_count} open candidates "
            f"({due_now_count} due now, {overdue_count} overdue), "
            f"{pending_approval_count} pending approvals."
        )

    @staticmethod
    def _render_finance_text(
        *,
        recent_memory_count: int,
        open_candidate_count: int,
        pending_approval_count: int,
        due_now_count: int,
        overdue_count: int,
    ) -> str:
        return (
            f"Finance brief: {recent_memory_count} finance-scoped memory items, "
            f"{open_candidate_count} open candidates "
            f"({due_now_count} due now, {overdue_count} overdue), "
            f"{pending_approval_count} pending approvals."
        )

    @staticmethod
    def _render_service_text(
        *,
        recent_picks_count: int,
        recent_overrides_count: int,
        open_pending_intents_count: int,
        recent_handoffs_count: int,
        active_threads_count: int,
        open_candidate_count: int,
        pending_approval_count: int,
        due_now_count: int,
        overdue_count: int,
    ) -> str:
        return (
            f"Service brief: {recent_picks_count} recent picks, "
            f"{recent_overrides_count} overrides, "
            f"{open_pending_intents_count} open pending intents, "
            f"{recent_handoffs_count} recent handoffs, "
            f"{active_threads_count} active threads, "
            f"{open_candidate_count} open candidates "
            f"({due_now_count} due now, {overdue_count} overdue), "
            f"{pending_approval_count} pending approvals."
        )

    @staticmethod
    def _is_fresh(last_built_at: datetime) -> bool:
        """Return True if last_built_at is within the freshness window."""
        if last_built_at is None:
            return False
        cutoff = _now_utc() - timedelta(seconds=_FRESHNESS_SECONDS)
        # Ensure tz-awareness
        lba = (
            last_built_at
            if last_built_at.tzinfo
            else last_built_at.replace(tzinfo=timezone.utc)
        )
        return lba > cutoff
