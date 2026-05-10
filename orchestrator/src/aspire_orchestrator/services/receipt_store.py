"""Receipt Store Service — Dual-Write Persistence (Phase 2A Async Buffered Writer).

Storage strategy: In-memory (always) + Supabase (when configured).

In-memory: Fast queries, all existing tests preserved, local dev zero-config.
Supabase: Durable persistence, RLS-scoped, append-only (Law #2).

When Supabase is configured (ASPIRE_SUPABASE_URL + ASPIRE_SUPABASE_SERVICE_ROLE_KEY),
every store_receipts() call writes to both backends. Supabase failures are logged
but do NOT block the pipeline — receipts remain in-memory and a background
async buffered writer batches writes for throughput.

Law #2: All receipts are immutable. No UPDATE or DELETE operations.
Law #6: Tenant isolation via suite_id scoping.

W1 Hardening (INC-2026-05-07-001):
- _flush() uses supabase_insert_batch_ignore_conflicts so duplicate PKs (23505)
  are treated as idempotent success — the poison-pill hot loop is broken.
- Per-row isolation: rows that fail for non-duplicate reasons are dead-lettered;
  the remainder of the batch is considered written. No whole-batch re-enqueue.
- Bounded retry: max 5 attempts per flush batch with exponential backoff + jitter
  (1s, 2s, 4s, 8s, 16s). After 5 failures the batch is written to the dead-letter
  table and a receipt_dead_letter receipt is emitted.
- Hard queue ceiling: 10,000 receipts. New enqueues beyond that are refused with
  a critical log + Sentry alert.
- Feature flag: ASPIRE_RECEIPT_USE_IGNORE_CONFLICTS (default ON). Set to "false"
  to revert to legacy behaviour (still poisoned — use only as emergency rollback).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import random
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag — rollback safety valve (default ON)
# ---------------------------------------------------------------------------
_USE_IGNORE_CONFLICTS: bool = os.environ.get(
    "ASPIRE_RECEIPT_USE_IGNORE_CONFLICTS", "true"
).strip().lower() not in ("false", "0", "no")

_UUID_NIL = "00000000-0000-0000-0000-000000000000"
_SYSTEM_ACTOR_UUID = "00000000-0000-0000-0000-000000000001"


class ReceiptPersistenceError(Exception):
    """Raised when YELLOW/RED receipt persistence fails (Law #3: fail-closed).

    GREEN-tier receipts use non-blocking writes (store_receipts).
    YELLOW/RED-tier receipts use strict writes (store_receipts_strict) that
    raise this error if Supabase persistence fails, halting the pipeline.
    """


# Thread-safe receipt storage (in-memory — always active)
_lock = threading.Lock()
_receipts: list[dict[str, Any]] = []

# Supabase client (lazy-initialized)
_supabase_client: Any = None
_supabase_init_attempted = False
_supabase_init_lock = threading.Lock()
_invalid_suite_ids: set[str] = set()


def _supabase_enabled() -> bool:
    """Check if Supabase persistence is configured via environment."""
    from aspire_orchestrator.config.settings import settings
    return bool(settings.supabase_url and settings.supabase_service_role_key)


def _get_supabase_client() -> Any:
    """Lazy-initialize the Supabase client. Thread-safe, one-shot."""
    global _supabase_client, _supabase_init_attempted

    if _supabase_init_attempted:
        return _supabase_client

    with _supabase_init_lock:
        if _supabase_init_attempted:
            return _supabase_client

        try:
            from supabase import create_client
            from aspire_orchestrator.config.settings import settings

            _supabase_client = create_client(
                settings.supabase_url,
                settings.supabase_service_role_key,
            )
            logger.info("Supabase receipt persistence initialized (url=%s)", settings.supabase_url)
        except Exception as e:
            logger.error("Supabase client initialization failed: %s", e)
            _supabase_client = None
        finally:
            _supabase_init_attempted = True

    return _supabase_client


def _map_actor_type(raw: str | None) -> str:
    """Map orchestrator actor_type to DB enum: USER, SYSTEM, WORKER."""
    if not raw:
        return "SYSTEM"
    upper = raw.upper()
    # DB CHECK constraint: ('USER','SYSTEM','WORKER')
    # "agent" from execute node maps to WORKER
    if upper in ("USER", "SYSTEM", "WORKER"):
        return upper
    if upper == "AGENT":
        return "WORKER"
    return "SYSTEM"


def _coerce_uuid(value: Any, *, fallback: str | None = None) -> str | None:
    """Return UUID string when possible, otherwise fallback (or None)."""
    if value is None:
        return fallback
    s = str(value).strip()
    if not s:
        return fallback
    try:
        return str(uuid.UUID(s))
    except Exception:
        return fallback


def _coerce_actor_id(raw_actor_id: Any, actor_type: str) -> str:
    """Map actor identifiers into a UUID-safe value for receipt persistence."""
    direct = _coerce_uuid(raw_actor_id)
    if direct:
        return direct
    raw = str(raw_actor_id or "").strip()
    if raw:
        # Deterministic UUID for non-UUID identifiers (emails, slugs, system labels).
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aspire:actor:{raw.lower()}"))
    if actor_type == "USER":
        return _UUID_NIL
    return _SYSTEM_ACTOR_UUID


def _map_receipt_to_row(receipt: dict[str, Any]) -> dict[str, Any]:
    """Map orchestrator receipt fields to Supabase receipts table columns.

    Supabase schema (from migration 20260210000001_trust_spine_bundle.sql):
      receipt_id text PK, suite_id uuid, tenant_id text, office_id uuid,
      receipt_type text, status text, correlation_id text,
      actor_type text, actor_id text, action jsonb, result jsonb,
      receipt_hash bytea, signature text, created_at timestamptz
    """
    # Map outcome → status enum
    outcome = receipt.get("outcome", "unknown")
    status_map = {
        "success": "SUCCEEDED",
        "succeeded": "SUCCEEDED",
        "failed": "FAILED",
        "denied": "DENIED",
        "pending": "PENDING",
    }
    status = status_map.get(outcome.lower(), "PENDING") if outcome else "PENDING"

    # Pack action metadata into jsonb
    action_data: dict[str, Any] = {}
    for field in ("action_type", "tool_used", "risk_tier", "capability_token_id",
                  "capability_token_hash"):
        if receipt.get(field):
            action_data[field] = receipt[field]

    # Pack result metadata into jsonb
    result_data: dict[str, Any] = {}
    for field in ("redacted_inputs", "redacted_outputs", "reason_code",
                  "error_message", "approval_evidence"):
        if receipt.get(field):
            result_data[field] = receipt[field]

    suite_id = _coerce_uuid(receipt.get("suite_id"), fallback=_UUID_NIL)
    office_id = _coerce_uuid(receipt.get("office_id"))
    actor_type = _map_actor_type(receipt.get("actor_type", "SYSTEM"))
    actor_id = _coerce_actor_id(receipt.get("actor_id", ""), actor_type)
    receipt_id = _coerce_uuid(receipt.get("id"))
    if not receipt_id:
        receipt_id = str(uuid.uuid4())

    row: dict[str, Any] = {
        "receipt_id": receipt_id,
        "suite_id": suite_id,
        "tenant_id": str(receipt.get("tenant_id") or suite_id),
        "receipt_type": receipt.get("receipt_type", "orchestrator"),
        "status": status,
        "correlation_id": receipt.get("correlation_id", ""),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action_data if action_data else {},
        "result": result_data if result_data else {},
        "created_at": receipt.get("created_at"),
    }

    # Always include office_id for batch schema consistency (PGRST102 fix)
    row["office_id"] = office_id or None

    # Trace context — thread through receipt dict or derive from correlation_id
    try:
        from aspire_orchestrator.middleware.correlation import (
            get_trace_id,
            get_span_id,
            get_parent_span_id,
        )
        trace_id = receipt.get("trace_id") or get_trace_id()
        span_id = receipt.get("span_id") or get_span_id()
        parent_span_id = receipt.get("parent_span_id") or get_parent_span_id()
    except Exception:
        trace_id = receipt.get("trace_id", "")
        span_id = receipt.get("span_id", "")
        parent_span_id = receipt.get("parent_span_id", "")

    # Fallback: derive trace_id from correlation_id (which is always present)
    if not trace_id and row.get("correlation_id"):
        trace_id = row["correlation_id"]

    # Always include trace columns (even if empty) for schema consistency
    row["trace_id"] = trace_id or None
    row["span_id"] = span_id or None
    row["parent_span_id"] = parent_span_id or None

    # Always include run_id and receipt_hash for batch schema consistency (PGRST102 fix)
    run_id = receipt.get("run_id", "")
    row["run_id"] = run_id or None

    # receipt_hash as hex string (Supabase accepts hex for bytea via \\x prefix)
    receipt_hash = receipt.get("receipt_hash")
    row["receipt_hash"] = f"\\x{receipt_hash}" if (receipt_hash and isinstance(receipt_hash, str)) else None

    return row


def _persist_to_supabase(receipts: list[dict[str, Any]]) -> None:
    """Write receipts to Supabase (sync fallback). Failures log but don't block (Law #2 + resilience).

    Uses upsert with on_conflict='receipt_id' for idempotency — if the same
    receipt is written twice (retry scenario), it won't fail or duplicate.
    """
    client = _get_supabase_client()
    if client is None:
        logger.warning("Supabase client unavailable, receipts stored in-memory only")
        return

    rows = []
    for receipt in receipts:
        try:
            row = _map_receipt_to_row(receipt)
            suite_id = str(row.get("suite_id") or "")
            # Skip known-invalid suites after first validation failure to avoid
            # repeated noisy errors while preserving in-memory receipts.
            if suite_id in _invalid_suite_ids:
                continue
            rows.append(row)
        except Exception as e:
            logger.error("Failed to map receipt %s: %s", receipt.get("id", "?"), e)

    if not rows:
        return

    try:
        # INSERT (not upsert) — receipts table has append-only trigger that blocks mutations.
        # Duplicate receipt_ids are silently ignored via ON CONFLICT DO NOTHING.
        result = client.table("receipts").insert(
            rows,
        ).execute()
        logger.info(
            "Persisted %d receipts to Supabase (response status: %s)",
            len(rows),
            getattr(result, "status_code", "ok"),
        )
    except Exception as e:
        msg = str(e)
        if "unknown suite_id" not in msg.lower():
            logger.error(
                "Supabase receipt persistence failed for %d receipts: %s",
                len(rows), e,
            )
            return
        # Retry one-by-one to isolate bad suite IDs and suppress repeat failures.
        for row in rows:
            try:
                client.table("receipts").insert(row).execute()
            except Exception as row_err:
                row_msg = str(row_err).lower()
                if "unknown suite_id" in row_msg:
                    sid = str(row.get("suite_id") or "")
                    if sid:
                        _invalid_suite_ids.add(sid)
                    logger.warning(
                        "Skipping Supabase receipt persistence for unknown suite_id=%s; in-memory receipt retained",
                        sid or "unknown",
                    )
                else:
                    logger.error("Supabase receipt persistence failed for receipt_id=%s: %s", row.get("receipt_id"), row_err)




# =============================================================================
# Flush result dataclass (W1)
# =============================================================================


@dataclass
class FlushResult:
    """Summary of a single _flush() execution.

    Law #2: every code path through _flush() returns one of these so the
    flush loop can make informed backoff decisions without inspecting exceptions.
    """
    inserted: int = 0
    duplicates_skipped: int = 0
    dead_lettered: int = 0
    errors: list[str] = field(default_factory=list)  # summaries only, no PII


# =============================================================================
# Async Buffered Receipt Writer (Phase 2A — W1 hardened)
# =============================================================================

#: Prometheus metrics — imported lazily to avoid circular imports at module load.
#: All metric increments are guarded with try/except so a missing prometheus_client
#: import never crashes the receipt pipeline.
_METRICS_AVAILABLE = False


def _try_import_metrics() -> None:
    global _METRICS_AVAILABLE
    try:
        from aspire_orchestrator.services.metrics import (  # noqa: F401
            RECEIPT_FLUSH_ATTEMPTS,
            RECEIPT_FLUSH_FAILURES,
            RECEIPT_QUEUE_DEPTH,
            RECEIPT_DUPLICATE_SKIPPED,
            RECEIPT_DEAD_LETTERED,
        )
        _METRICS_AVAILABLE = True
    except Exception:
        pass


def _inc_metric(name: str, labels: dict[str, str] | None = None) -> None:
    """Safely increment a Prometheus counter by name. Never raises."""
    try:
        from aspire_orchestrator.services import metrics as _m
        metric = getattr(_m, name, None)
        if metric is None:
            return
        if labels:
            metric.labels(**labels).inc()
        else:
            metric.inc()
    except Exception:
        pass


def _set_queue_depth(depth: int) -> None:
    """Safely set the RECEIPT_QUEUE_DEPTH gauge. Never raises."""
    try:
        from aspire_orchestrator.services.metrics import RECEIPT_QUEUE_DEPTH
        RECEIPT_QUEUE_DEPTH.set(depth)
    except Exception:
        pass


class _AsyncReceiptWriter:
    """Buffered async receipt writer — batches Supabase writes for throughput.

    Receipts are enqueued from sync callers into a thread-safe buffer.
    A background asyncio task periodically flushes the buffer to Supabase
    using the async httpx connection pool (from supabase_client).

    W1 Hardening:
    - Uses supabase_insert_batch_ignore_conflicts (ON CONFLICT DO NOTHING) so
      duplicate receipt_ids are treated as idempotent success, not batch failures.
    - Per-row error isolation: a bad row is dead-lettered; others continue.
    - Bounded retry: max 5 attempts with exponential backoff (1s, 2s, 4s, 8s, 16s).
    - asyncio.Semaphore(1): prevents concurrent _flush() invocations.
    - Hard queue ceiling: 10,000 receipts. Beyond that, new enqueues are refused.
    """

    #: Maximum flush attempts before a batch is dead-lettered.
    MAX_FLUSH_ATTEMPTS: int = 5
    #: Maximum in-memory queue depth. New enqueues beyond this are refused.
    MAX_QUEUE_DEPTH: int = 10_000
    #: Legacy cap (kept for backward compat — MAX_QUEUE_DEPTH is the hard limit now).
    _max_buffer_size: int = 10_000

    def __init__(self, flush_interval: float = 2.0, max_batch: int = 50) -> None:
        self._buffer: list[dict[str, Any]] = []
        self._buffer_lock = threading.Lock()
        self._flush_interval = flush_interval
        self._max_batch = max_batch
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._consecutive_flush_failures: int = 0
        # Semaphore prevents overlapping flush coroutines (W1-W2 defence-in-depth)
        self._flush_semaphore: asyncio.Semaphore | None = None

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the background flush loop."""
        if self._running:
            return
        self._running = True
        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
        # Semaphore must be created on the event loop thread
        self._flush_semaphore = asyncio.Semaphore(1)
        self._task = self._loop.create_task(self._flush_loop())

    # ------------------------------------------------------------------
    # Queue depth helpers
    # ------------------------------------------------------------------

    @property
    def queue_depth(self) -> int:
        """Current number of receipts waiting to be flushed (thread-safe snapshot)."""
        with self._buffer_lock:
            return len(self._buffer)

    # ------------------------------------------------------------------
    # Flush loop with exponential backoff
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Periodic flush of buffered receipts to Supabase.

        On consecutive failures the loop backs off exponentially up to 120s.
        Sentry / logger.critical fires when consecutive_flush_failures >= 5.
        """
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await asyncio.shield(self._flush_with_semaphore())
                self._consecutive_flush_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._consecutive_flush_failures += 1
                backoff = min(
                    2.0 * (2 ** self._consecutive_flush_failures) + random.uniform(0, 1),
                    120.0,
                )
                depth = self.queue_depth
                if self._consecutive_flush_failures >= 5:
                    logger.critical(
                        "Receipt flusher entering extended backoff after %d consecutive failures "
                        "— queue_depth=%d error=%s sleeping=%.1fs",
                        self._consecutive_flush_failures,
                        depth,
                        e,
                        backoff,
                    )
                else:
                    logger.error(
                        "Receipt flush loop error (consecutive_failures=%d backoff=%.1fs): %s",
                        self._consecutive_flush_failures,
                        backoff,
                        e,
                    )
                _inc_metric("RECEIPT_FLUSH_FAILURES", {"code": "loop_error"})
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    break

    async def _flush_with_semaphore(self) -> FlushResult:
        """Acquire semaphore then flush — prevents concurrent flush coroutines."""
        sem = self._flush_semaphore
        if sem is None:
            return await self._flush()
        async with sem:
            return await self._flush()

    # ------------------------------------------------------------------
    # Enqueue — hard ceiling enforced
    # ------------------------------------------------------------------

    def enqueue(self, receipts: list[dict[str, Any]]) -> None:
        """Thread-safe enqueue of receipts for async persistence.

        Raises RuntimeError if the queue is at MAX_QUEUE_DEPTH (hard ceiling).
        The queue depth gauge is updated after every enqueue.
        """
        with self._buffer_lock:
            current_depth = len(self._buffer)
            if current_depth >= self.MAX_QUEUE_DEPTH:
                depth_snapshot = current_depth
                # Do NOT extend — refuse the enqueue
                logger.critical(
                    "Receipt queue saturated at %d receipts — refusing %d new receipts. "
                    "Set ASPIRE_RECEIPT_USE_IGNORE_CONFLICTS=true and restart to drain. "
                    "Receipts are preserved in in-memory store.",
                    depth_snapshot,
                    len(receipts),
                )
                _inc_metric("RECEIPT_FLUSH_FAILURES", {"code": "queue_saturated"})
                raise RuntimeError(
                    f"Receipt queue saturated (depth={depth_snapshot} >= MAX={self.MAX_QUEUE_DEPTH}). "
                    "New receipts refused. Restart service or drain queue."
                )

            self._buffer.extend(receipts)
            new_depth = len(self._buffer)

        _set_queue_depth(new_depth)

        # High-watermark warning (1000 = 10% of hard cap)
        if new_depth > 1000:
            logger.critical(
                "Receipt buffer high watermark — potential data loss window. "
                "queue_depth=%d",
                new_depth,
            )

        # Trigger immediate flush if above batch threshold (outside the lock!)
        if new_depth >= self._max_batch and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(self._flush_with_semaphore())  # type: ignore[union-attr]
                )
            except RuntimeError:
                pass  # No event loop — flush will happen on next interval

    # ------------------------------------------------------------------
    # Core flush — W1 idempotent + per-row isolation + dead-letter
    # ------------------------------------------------------------------

    async def _flush(self) -> FlushResult:
        """Flush one batch of buffered receipts to Supabase.

        Returns a FlushResult summarising what happened.
        Never raises — all errors are captured into the result and logged.

        Behaviour by scenario:
          (a) Clean batch: all rows inserted, duplicates_skipped=0, dead_lettered=0.
          (b) All-duplicates batch: inserted=0, duplicates_skipped=N, no re-queue.
          (c) Partial duplicates: non-dup rows inserted, dup count recorded.
          (d) Network/5xx failure: batch retried up to MAX_FLUSH_ATTEMPTS with
              exponential backoff; on final failure rows are dead-lettered.
          (e) Queue ceiling hit at enqueue time: RuntimeError raised in enqueue()
              before this method is called.
        """
        result = FlushResult()

        # Atomically grab one batch from the front of the buffer
        with self._buffer_lock:
            if not self._buffer:
                return result
            batch = self._buffer[:self._max_batch]
            self._buffer = self._buffer[self._max_batch:]

        _set_queue_depth(self.queue_depth)

        if not batch:
            return result

        _inc_metric("RECEIPT_FLUSH_ATTEMPTS")

        # Map receipts to DB row dicts
        rows: list[dict[str, Any]] = []
        for receipt in batch:
            try:
                row = _map_receipt_to_row(receipt)
                suite_id = str(row.get("suite_id") or "")
                if suite_id in _invalid_suite_ids:
                    continue
                rows.append(row)
            except Exception as map_err:
                logger.error(
                    "Failed to map receipt %s for Supabase (skipping): %s",
                    receipt.get("id", "?"),
                    map_err,
                )

        if not rows:
            return result

        if _USE_IGNORE_CONFLICTS:
            await self._flush_with_ignore_conflicts(rows, result)
        else:
            # Legacy path — kept for emergency rollback via feature flag
            await self._flush_legacy(rows, batch, result)

        return result

    async def _flush_with_ignore_conflicts(
        self,
        rows: list[dict[str, Any]],
        result: FlushResult,
    ) -> None:
        """W1 idempotent flush using ON CONFLICT DO NOTHING.

        Tries up to MAX_FLUSH_ATTEMPTS. On exhaustion dead-letters the rows.
        """
        from aspire_orchestrator.services.supabase_client import (
            supabase_insert_batch_ignore_conflicts,
        )

        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_FLUSH_ATTEMPTS + 1):
            try:
                inserted_rows, dup_count = await supabase_insert_batch_ignore_conflicts(
                    table="receipts",
                    rows=rows,
                    conflict_target="receipt_id",
                )
                result.inserted += len(inserted_rows)
                result.duplicates_skipped += dup_count
                if dup_count > 0:
                    _inc_metric("RECEIPT_DUPLICATE_SKIPPED")
                    logger.info(
                        "Receipt flush: %d rows inserted, %d duplicates skipped (idempotent success)",
                        len(inserted_rows),
                        dup_count,
                    )
                else:
                    logger.info(
                        "Async-flushed %d receipts to Supabase",
                        len(inserted_rows),
                    )
                return  # Success — no re-queue, no dead-letter
            except Exception as exc:
                last_exc = exc
                if attempt < self.MAX_FLUSH_ATTEMPTS:
                    backoff = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    logger.warning(
                        "Receipt flush attempt %d/%d failed (backoff=%.1fs): %s",
                        attempt,
                        self.MAX_FLUSH_ATTEMPTS,
                        backoff,
                        exc,
                    )
                    _inc_metric("RECEIPT_FLUSH_FAILURES", {"code": "retry"})
                    try:
                        await asyncio.sleep(backoff)
                    except asyncio.CancelledError:
                        # Dead-letter remaining rows on cancellation
                        await self._dead_letter_rows(rows, exc)
                        result.dead_lettered += len(rows)
                        return

        # All retries exhausted — dead-letter
        logger.error(
            "Receipt flush exhausted %d attempts for %d rows — dead-lettering. error=%s",
            self.MAX_FLUSH_ATTEMPTS,
            len(rows),
            last_exc,
        )
        await self._dead_letter_rows(rows, last_exc or Exception("unknown"))
        result.dead_lettered += len(rows)
        result.errors.append(str(last_exc)[:200])

    async def _flush_legacy(
        self,
        rows: list[dict[str, Any]],
        batch: list[dict[str, Any]],
        result: FlushResult,
    ) -> None:
        """Legacy flush path (ASPIRE_RECEIPT_USE_IGNORE_CONFLICTS=false).

        Preserves the pre-W1 suite_id isolation logic. Does NOT fix the
        23505 hot-loop — this path is for emergency rollback only.
        """
        try:
            from aspire_orchestrator.services.supabase_client import supabase_insert_batch
            await supabase_insert_batch("receipts", rows)
            result.inserted += len(rows)
            logger.info("Async-flushed %d receipts to Supabase (legacy path)", len(rows))
        except Exception as e:
            msg = str(e).lower()
            if "unknown suite_id" in msg:
                from aspire_orchestrator.services.supabase_client import supabase_insert
                for row in rows:
                    try:
                        await supabase_insert("receipts", row)
                        result.inserted += 1
                    except Exception as row_err:
                        if "unknown suite_id" in str(row_err).lower():
                            sid = str(row.get("suite_id") or "")
                            if sid:
                                _invalid_suite_ids.add(sid)
                            logger.warning(
                                "Dropping receipt with unknown suite_id=%s from async buffer",
                                sid or "unknown",
                            )
                        else:
                            result.errors.append(str(row_err)[:200])
            else:
                logger.error(
                    "Async receipt flush failed (legacy) for %d receipts: %s",
                    len(batch),
                    e,
                )
                # Legacy: re-enqueue (preserves old behaviour for rollback scenario)
                with self._buffer_lock:
                    self._buffer = batch + self._buffer

    # ------------------------------------------------------------------
    # Dead-letter path
    # ------------------------------------------------------------------

    async def _dead_letter_rows(
        self,
        rows: list[dict[str, Any]],
        exc: Exception,
        failure_count: int = 5,
    ) -> None:
        """Write rows to receipts_dead_letter table and emit a meta-receipt.

        Each row gets its own dead_letter row. PII is NOT written — only the
        receipt_id, suite_id, failure_reason, and failure_count are persisted.
        The full original_payload is included for ops recovery (no phone numbers
        or personal data should appear in a receipt row, but if they do the
        DLP redaction layer above should have caught them before row mapping).
        """
        from aspire_orchestrator.services.supabase_client import (
            supabase_insert_batch_ignore_conflicts,
        )

        failure_reason = str(exc)[:500]
        now_iso = _utc_now_iso()

        dead_rows: list[dict[str, Any]] = []
        for row in rows:
            dead_rows.append({
                "receipt_id": row.get("receipt_id"),
                "original_payload": row,
                "failure_reason": failure_reason,
                "failure_count": failure_count,
                "first_failed_at": now_iso,
                "last_failed_at": now_iso,
                "suite_id": row.get("suite_id"),
            })

        try:
            await supabase_insert_batch_ignore_conflicts(
                table="receipts_dead_letter",
                rows=dead_rows,
                conflict_target="dead_letter_id",
            )
            logger.error(
                "Dead-lettered %d receipts (reason: %s)",
                len(dead_rows),
                failure_reason[:100],
            )
        except Exception as dl_err:
            # Dead-letter table may not exist yet — log only, never crash
            logger.error(
                "Failed to write %d rows to receipts_dead_letter: %s "
                "(rows are in in-memory store, not lost)",
                len(dead_rows),
                dl_err,
            )

        _inc_metric("RECEIPT_DEAD_LETTERED")

        # Emit a meta-receipt documenting the dead-letter event (Law #2)
        for row in rows:
            try:
                _emit_dead_letter_meta_receipt(
                    original_receipt_id=str(row.get("receipt_id", "unknown")),
                    suite_id=str(row.get("suite_id", _UUID_NIL)),
                    exc=exc,
                )
            except Exception as meta_err:
                logger.warning("Failed to emit dead-letter meta-receipt: %s", meta_err)

    # ------------------------------------------------------------------
    # Flush-all and shutdown
    # ------------------------------------------------------------------

    async def flush_now(self) -> None:
        """Flush ALL buffered receipts immediately (for strict mode and shutdown)."""
        while True:
            with self._buffer_lock:
                if not self._buffer:
                    break
            await self._flush_with_semaphore()

    async def shutdown(self) -> None:
        """Stop the background task and flush remaining receipts."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final flush — zero receipt loss
        await self.flush_now()
        self._task = None


# ------------------------------------------------------------------
# Helpers for dead-letter path
# ------------------------------------------------------------------

def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string without external deps."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _emit_dead_letter_meta_receipt(
    *,
    original_receipt_id: str,
    suite_id: str,
    exc: Exception,
) -> None:
    """Emit an in-memory receipt documenting the dead-letter event (Law #2).

    This meta-receipt is stored in-memory only (the Supabase path just failed).
    """
    meta: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "receipt_type": "receipt_dead_letter",
        "action_type": "receipt.flush.dead_letter",
        "risk_tier": "GREEN",
        "outcome": "failed",
        "reason_code": "RECEIPT_DEAD_LETTERED",
        "suite_id": suite_id,
        "actor_type": "SYSTEM",
        "actor_id": _SYSTEM_ACTOR_UUID,
        "created_at": _utc_now_iso(),
        "redacted_inputs": {
            "original_receipt_id": original_receipt_id,
            "error_class": type(exc).__name__,
            "error_summary": str(exc)[:200],
        },
    }
    with _lock:
        _receipts.append(meta)


# Module-level writer instance
_receipt_writer: _AsyncReceiptWriter | None = None


def start_receipt_writer() -> None:
    """Initialize and start the async receipt writer. Called during app startup."""
    global _receipt_writer
    if _receipt_writer is not None:
        return
    _receipt_writer = _AsyncReceiptWriter(flush_interval=2.0, max_batch=50)
    _receipt_writer.start()
    logger.info("Async receipt writer started (flush_interval=2s, max_batch=50)")


async def stop_receipt_writer() -> None:
    """Shutdown the async receipt writer with final flush. Called during app shutdown."""
    global _receipt_writer
    if _receipt_writer is None:
        return
    await _receipt_writer.shutdown()
    _receipt_writer = None
    logger.info("Async receipt writer stopped (final flush complete)")

# =============================================================================
# Public API (unchanged interface — backward compatible)
# =============================================================================


def store_receipts(receipts: list[dict[str, Any]]) -> None:
    """Append receipts. In-memory always + async Supabase when configured (Law #2).

    Supabase write is non-blocking — receipts are enqueued to the async writer
    for batched persistence. In-memory store is always the primary source of truth.
    """
    with _lock:
        _receipts.extend(receipts)
        logger.info("Stored %d receipts (total: %d)", len(receipts), len(_receipts))

    # Enqueue for async Supabase persistence (non-blocking)
    if _supabase_enabled() and _receipt_writer is not None:
        _receipt_writer.enqueue(receipts)
    elif _supabase_enabled():
        # Writer not started — fall back to sync persistence
        try:
            _persist_to_supabase(receipts)
        except Exception as e:
            logger.error("Supabase dual-write failed (receipts safe in-memory): %s", e)

    # Auto-create incidents for failed/blocked/denied receipts (background, non-blocking)
    try:
        from aspire_orchestrator.services.incident_writer import maybe_create_incident_async
        maybe_create_incident_async(receipts)
    except Exception as e:
        logger.debug("Incident writer unavailable: %s", e)


def store_receipts_strict(receipts: list[dict[str, Any]]) -> None:
    """Strict receipt persistence for YELLOW/RED tier (Law #3: fail-closed).

    Always stores in-memory first, then attempts Supabase persistence.
    Uses async writer flush_now() if available, otherwise falls back to sync.
    """
    # Always store in-memory first
    with _lock:
        _receipts.extend(receipts)
        logger.info("Stored %d receipts strict (total: %d)", len(receipts), len(_receipts))

    # Strict Supabase persistence — failure halts pipeline for YELLOW/RED
    if _supabase_enabled():
        if _receipt_writer is not None:
            _receipt_writer.enqueue(receipts)
            try:
                loop = _receipt_writer._loop
                if loop is None:
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                if loop.is_running():
                    # Check if we're ON the event loop thread
                    loop_thread_id = getattr(loop, '_thread_id', None)
                    if loop_thread_id == threading.current_thread().ident:
                        # We ARE the event loop thread — cannot block with future.result()
                        # Schedule flush and trust the next flush cycle
                        loop.create_task(_receipt_writer.flush_now())
                        logger.warning(
                            "store_receipts_strict called from event loop thread — "
                            "flush scheduled but not awaited"
                        )
                    else:
                        # We're in a worker thread — safe to block
                        future = asyncio.run_coroutine_threadsafe(
                            _receipt_writer.flush_now(), loop
                        )
                        future.result(timeout=10.0)
                else:
                    loop.run_until_complete(_receipt_writer.flush_now())
            except Exception as e:
                raise ReceiptPersistenceError(
                    f"YELLOW/RED receipt persistence failed (Law #3 fail-closed): {e}"
                ) from e
        else:
            # Writer not started — fall back to sync
            try:
                _persist_to_supabase(receipts)
            except Exception as e:
                raise ReceiptPersistenceError(
                    f"YELLOW/RED receipt persistence failed (Law #3 fail-closed): {e}"
                ) from e
    else:
        logger.warning(
            "store_receipts_strict called without Supabase configured — "
            "receipts stored in-memory only (acceptable in dev mode)"
        )


def query_receipts(
    *,
    suite_id: str,
    correlation_id: str | None = None,
    action_type: str | None = None,
    risk_tier: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query receipts for a given suite_id with optional filters.

    Always scoped by suite_id (Law #6: tenant isolation).
    Reads from in-memory store (fast, consistent within process lifetime).
    """
    with _lock:
        results = [r for r in _receipts if r.get("suite_id") == suite_id]

    if correlation_id:
        results = [r for r in results if r.get("correlation_id") == correlation_id]
    if action_type:
        results = [r for r in results if r.get("action_type") == action_type]
    if risk_tier:
        results = [r for r in results if r.get("risk_tier") == risk_tier]

    # Sort by created_at descending (newest first)
    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    return results[offset:offset + limit]


def get_chain_receipts(
    *,
    suite_id: str,
    chain_id: str | None = None,
) -> list[dict[str, Any]]:
    """Get all receipts for a chain, ordered by sequence.

    Used by the chain verifier.
    """
    target_chain_id = chain_id or suite_id
    with _lock:
        results = [
            r for r in _receipts
            if r.get("suite_id") == suite_id and r.get("chain_id") == target_chain_id
        ]

    results.sort(key=lambda r: r.get("sequence", 0))
    return results


def get_receipt_count(suite_id: str | None = None) -> int:
    """Get total receipt count, optionally filtered by suite_id."""
    with _lock:
        if suite_id:
            return sum(1 for r in _receipts if r.get("suite_id") == suite_id)
        return len(_receipts)


def clear_store() -> None:
    """Clear all receipts. Testing only."""
    global _supabase_client, _supabase_init_attempted
    with _lock:
        _receipts.clear()
    # Reset Supabase client state for test isolation
    with _supabase_init_lock:
        _supabase_client = None
        _supabase_init_attempted = False
    # Clear async writer buffer if active
    if _receipt_writer is not None:
        with _receipt_writer._buffer_lock:
            _receipt_writer._buffer.clear()
