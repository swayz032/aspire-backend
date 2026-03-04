"""Outbox Client with durable Supabase backend and dev memory fallback."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_rpc,
    supabase_select,
)

logger = logging.getLogger(__name__)


class OutboxJobStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class OutboxJob(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    suite_id: str
    office_id: str
    correlation_id: str
    action_type: str
    risk_tier: str = "red"
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    capability_token_id: str | None = None
    status: OutboxJobStatus = OutboxJobStatus.PENDING
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    max_retries: int = 3
    retry_count: int = 0


class OutboxSubmitResult(BaseModel):
    success: bool
    job_id: str
    status: OutboxJobStatus = OutboxJobStatus.PENDING
    error: str | None = None
    receipt: dict[str, Any] | None = None


def _map_status(status: str | None) -> OutboxJobStatus:
    value = (status or "").strip().upper()
    return {
        "QUEUED": OutboxJobStatus.PENDING,
        "RUNNING": OutboxJobStatus.CLAIMED,
        "SUCCEEDED": OutboxJobStatus.COMPLETED,
        "FAILED": OutboxJobStatus.FAILED,
        "DEAD": OutboxJobStatus.DEAD_LETTER,
    }.get(value, OutboxJobStatus.PENDING)


class OutboxClient:
    """Outbox backed by Supabase when available, memory only for local fallback."""

    def __init__(self) -> None:
        self._jobs: dict[str, OutboxJob] = {}
        self.backend = "memory"
        if settings.supabase_url and settings.supabase_service_role_key:
            self.backend = "supabase"
        logger.info("OutboxClient initialized backend=%s", self.backend)

    async def submit_job(self, job: OutboxJob) -> OutboxSubmitResult:
        if not job.suite_id:
            return OutboxSubmitResult(success=False, job_id=job.job_id, error="missing_suite_id")

        if self.backend == "supabase":
            try:
                row = {
                    "id": job.job_id,
                    "suite_id": job.suite_id,
                    "action_type": job.action_type,
                    "idempotency_key": job.idempotency_key or f"{job.correlation_id}:{job.action_type}:{job.job_id}",
                    "status": "QUEUED",
                    "payload": {
                        **job.payload,
                        "office_id": job.office_id,
                        "correlation_id": job.correlation_id,
                        "risk_tier": job.risk_tier,
                        "capability_token_id": job.capability_token_id,
                        "max_retries": job.max_retries,
                    },
                }
                await supabase_insert("outbox_jobs", row)
            except SupabaseClientError as exc:
                logger.error("Outbox submit failed on supabase backend: %s", exc)
                return OutboxSubmitResult(success=False, job_id=job.job_id, error=str(exc))
        else:
            self._jobs[job.job_id] = job

        receipt = {
            "receipt_version": "1.0",
            "receipt_id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": "outbox.job.submitted",
            "suite_id": job.suite_id,
            "office_id": job.office_id,
            "correlation_id": job.correlation_id,
            "actor": "service:outbox_client",
            "status": "ok",
            "data": {
                "job_id": job.job_id,
                "action_type": job.action_type,
                "risk_tier": job.risk_tier,
                "idempotency_key": job.idempotency_key,
            },
            "policy": {"decision": "allow", "policy_id": "outbox-v1", "reasons": []},
            "redactions": [],
        }

        return OutboxSubmitResult(
            success=True,
            job_id=job.job_id,
            status=OutboxJobStatus.PENDING,
            receipt=receipt,
        )

    async def get_job_status(self, job_id: str) -> OutboxJob | None:
        if self.backend == "supabase":
            try:
                rows = await supabase_select(
                    "outbox_jobs",
                    f"id=eq.{job_id}&select=id,suite_id,action_type,status,payload,created_at,attempt_count",
                )
            except SupabaseClientError:
                return None
            if not rows:
                return None
            row = rows[0]
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            return OutboxJob(
                job_id=str(row.get("id")),
                suite_id=str(row.get("suite_id")),
                office_id=str(payload.get("office_id") or ""),
                correlation_id=str(payload.get("correlation_id") or ""),
                action_type=str(row.get("action_type") or ""),
                risk_tier=str(payload.get("risk_tier") or "red"),
                payload=payload if isinstance(payload, dict) else {},
                idempotency_key=None,
                status=_map_status(str(row.get("status") or "")),
                created_at=str(row.get("created_at") or datetime.now(timezone.utc).isoformat()),
                retry_count=int(row.get("attempt_count") or 0),
            )
        return self._jobs.get(job_id)

    async def claim_job(self, job_id: str) -> bool:
        if self.backend == "supabase":
            # Claim specific job is not exposed as dedicated RPC; worker flow should use claim_outbox_jobs.
            # We only claim by suite for deterministic concurrency semantics.
            job = await self.get_job_status(job_id)
            if not job:
                return False
            try:
                rows = await supabase_rpc(
                    "claim_outbox_jobs",
                    {"p_suite_id": job.suite_id, "p_limit": 10, "p_worker_id": "orchestrator"},
                )
            except SupabaseClientError:
                return False
            return any(str(row.get("id")) == job_id for row in rows or [])

        local = self._jobs.get(job_id)
        if local and local.status == OutboxJobStatus.PENDING:
            local.status = OutboxJobStatus.CLAIMED
            return True
        return False

    async def complete_job(self, job_id: str, *, receipt_id: str | None = None) -> bool:
        if self.backend == "supabase":
            try:
                await supabase_rpc("complete_outbox_job", {"p_job_id": job_id})
                return True
            except SupabaseClientError:
                return False

        job = self._jobs.get(job_id)
        if job and job.status in (OutboxJobStatus.CLAIMED, OutboxJobStatus.EXECUTING):
            job.status = OutboxJobStatus.COMPLETED
            return True
        return False

    async def fail_job(self, job_id: str, *, error: str) -> bool:
        if self.backend == "supabase":
            try:
                await supabase_rpc("fail_outbox_job", {"p_job_id": job_id, "p_error": error})
                return True
            except SupabaseClientError:
                return False

        job = self._jobs.get(job_id)
        if not job:
            return False
        job.retry_count += 1
        if job.retry_count >= job.max_retries:
            job.status = OutboxJobStatus.DEAD_LETTER
        else:
            job.status = OutboxJobStatus.PENDING
        return True

    def get_queue_status(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if self.backend == "supabase":
            try:
                rows = self._select_outbox_rows_sync()
            except Exception:
                rows = []
            queue_depth = 0
            stuck_jobs = 0
            oldest_age = 0
            for row in rows:
                status = str(row.get("status") or "")
                if status in {"QUEUED", "RUNNING", "FAILED"}:
                    queue_depth += 1
                if status == "DEAD":
                    stuck_jobs += 1
                ts = row.get("created_at") or row.get("updated_at")
                try:
                    created = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    age = int((now - created).total_seconds())
                    oldest_age = max(oldest_age, age)
                    if status in {"QUEUED", "RUNNING"} and age > 300:
                        stuck_jobs += 1
                except Exception:
                    pass
            return {
                "queue_depth": queue_depth,
                "oldest_age_seconds": oldest_age,
                "stuck_jobs": stuck_jobs,
                "server_time": now.isoformat(),
                "backend": self.backend,
            }

        active = (OutboxJobStatus.PENDING, OutboxJobStatus.CLAIMED, OutboxJobStatus.EXECUTING)
        pending_jobs = [j for j in self._jobs.values() if j.status in active]
        dead = [j for j in self._jobs.values() if j.status == OutboxJobStatus.DEAD_LETTER]
        oldest_age = 0
        stuck_jobs = len(dead)
        for j in pending_jobs:
            try:
                created = datetime.fromisoformat(j.created_at)
                age = int((now - created).total_seconds())
                oldest_age = max(oldest_age, age)
                if age > 300:
                    stuck_jobs += 1
            except Exception:
                pass
        return {
            "queue_depth": len(pending_jobs),
            "oldest_age_seconds": oldest_age,
            "stuck_jobs": stuck_jobs,
            "server_time": now.isoformat(),
            "backend": self.backend,
        }

    def _select_outbox_rows_sync(self) -> list[dict[str, Any]]:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return []
        base = settings.supabase_url.rstrip("/")
        url = (
            f"{base}/rest/v1/outbox_jobs"
            "?select=id,status,created_at,updated_at"
            "&status=in.(QUEUED,RUNNING,FAILED,DEAD)"
            "&limit=1000"
        )
        headers = {
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
        }
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code >= 400:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []

    def clear_jobs(self) -> None:
        self._jobs.clear()


_client: OutboxClient | None = None


def get_outbox_client(*, reload: bool = False) -> OutboxClient:
    global _client
    if _client is None or reload:
        _client = OutboxClient()
    return _client
