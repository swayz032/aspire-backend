"""Pydantic v2 schemas for the Office Memory Engine + Coordination Spine V1.

Maps 1:1 with DB tables: memory_objects (096), threads (095),
proactive_candidates, approval_links, receipt_memory_links,
memory_event_inbox (097).

Source-of-truth for all column shapes: SQL migrations 095–097.

Divergences from plan idealized spec:
- embedding is vector(1536) matching settings.embedding_dimensions and migration 096.
- tenant_id / suite_id / office_id are UUID (not TEXT) on all V1 tables.
- approval_requests.approval_id is TEXT PK → ApprovalLinkIn.approval_id is str.
- receipts.receipt_id is TEXT PK → ReceiptMemoryLinkIn.receipt_id is str.

Law compliance:
  Law #3: Fail Closed — validators raise on invalid values (never silently default).
  Law #6: Tenant Isolation — ScopedIdentity carries all three scope fields (required).
  Law #9: Security — idempotency_key required on MemoryEventEnvelope (NOT NULL in DB).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Enum Literals (match DB CHECK constraints exactly)
# ---------------------------------------------------------------------------

MemoryType = Literal[
    # Original 14 types (Pass 1)
    "session_summary",
    "handoff_note",
    "pending_intent",
    "authority_context",
    "thread_summary",
    "office_brief",
    "finance_brief",
    "decision_fact",
    "risk_flag",
    "followup_task",
    "timeline_event",
    "artifact_reference",
    "receipt_reference",
    "workflow_reference",
    # Pass 14 ingestion extensions (migration 101)
    "invoice",       # Stripe invoice events
    "quote",         # PandaDoc + internal estimate-studio quotes
    "call",          # Twilio voice calls (recording + transcript)
    "meeting",       # Zoom meetings (recording + transcript)
    "transcript",    # Raw EL/Anam conversation transcripts
    "sms_thread",    # Twilio SMS threads (one per contact per office)
    # Pass 14 expansion (migration 103) — contracts, document uploads, calendar
    "contract",        # PandaDoc contracts (signed/declined) — distinct from quote
    "document",        # User-uploaded files (PDF / image / doc) via Aspire upload pipeline
    "calendar_event",  # Google Calendar / Outlook calendar events
]

MemoryStatus = Literal[
    "requested",
    "drafted",
    "pending_approval",
    "approved",
    "executed",
    "rejected",
    "superseded",
    "failed",
    "promoted",
]

RuntimeFamily = Literal[
    "elevenlabs",
    "anam",
    "internal",
    "ui",
    "provider_webhook",
]

Channel = Literal[
    "voice",
    "video",
    "email",
    "sms",
    "workflow",
    "finance",
    "ui",
    "webhook",
]

SourceSurface = Literal[
    "ava_voice",
    "sarah_voice",
    "eli_inbox",
    "nora_meeting",
    "finn_finance",
    "tim_service_lab",
    # General Estimate Studio surface (non-service-hub context)
    "estimate_studio",
    "canvas_desk",
    "receipt_ledger",
    "approval_queue",
    "system",
    # Pass 14 expansion — new ingestion surface origins
    "tec_documents",    # Aspire upload pipeline (user-uploaded files)
    "google_calendar",  # Google Calendar push notifications
    "aspire_calendar",  # Aspire internal calendar events
    # Wave 5.1b — Service Hub surfaces (service visibility scope)
    # Service Hub Estimate Studio with Drew context (use for service visibility_scope writes)
    "service_hub_estimate_studio",
    "service_hub_jobs",
    "service_hub_dispatch",
    "service_hub_scheduling",
    "service_hub_inspections",
    "internal_drew",
    "elevenlabs_tim_service",
    "anam_tim_service",
]

SourceAgent = Literal[
    "ava",
    "sarah",
    "eli",
    "nora",
    "finn",
    "tim",
    "system",
]

VisibilityScope = Literal[
    "office",
    "finance",
    "service",      # Wave 5.1b — Service Hub operational memory (Drew, jobs, dispatch).
                    # NOTE: DB CHECK constraint update required in Wave 5.1b-2 migration before writing.
    "workflow",
    "admin",
    "restricted",
]

RiskTier = Literal["green", "yellow", "red"]

ThreadType = Literal[
    "lead_thread",
    "customer_thread",
    "deal_thread",
    "job_thread",
    "project_thread",
    "property_thread",   # Wave 5.1b — all memory tied to a property address across projects/jobs
    "estimate_thread",
    "quote_thread",
    "invoice_thread",
    "contract_thread",
    "meeting_thread",
    "finance_thread",
    "task_thread",
    "internal_thread",
    "client_thread",
]

FinanceThreadSubtype = Literal[
    "collections_case",
    "provider_connection_issue",
    "reconciliation_cluster",
    "categorization_cluster",
    "payroll_review",
    "tax_review",
    "cash_risk_review",
    "invoice_aging_review",
    "finance_task",
    "finance_state_change",
    "payment_event",
]

RecommendedAction = Literal[
    "create_draft",
    "queue_callback",
    "request_approval",
    "route_to_agent",
    "queue_workflow_trigger",
    "surface_warning",
    "create_internal_task",
    "schedule_outbound_voice",
    "none",
]

ActionClass = Literal[
    "internal_only",
    "draft",
    "approval_request",
    "outbound",
    "workflow",
]

CandidateStatus = Literal[
    "open",
    "snoozed",
    "approved",
    "executed",
    "dismissed",
    "expired",
]

ApprovalStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "expired",
]

EventInboxStatus = Literal[
    "pending",
    "processing",
    "processed",
    "dead_letter",
]

ThreadStatus = Literal["open", "closed", "archived"]

# ---------------------------------------------------------------------------
# Composite value objects
# ---------------------------------------------------------------------------


class ScopedIdentity(BaseModel):
    """Tenant + suite + office scope envelope. All three are required (Law #6)."""

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    actor_id: UUID | None = None
    user_id: UUID | None = None


class Provenance(BaseModel):
    """Flattened provenance block. trace_id and correlation_id are always required."""

    source_surface: SourceSurface | None = None
    source_agent: SourceAgent | None = None
    runtime_family: RuntimeFamily | None = None
    channel: Channel | None = None
    session_provider: str | None = None
    transcript_provider: str | None = None
    recording_provider: str | None = None
    external_session_id: str | None = None
    source_record_id: str | None = None
    trace_id: UUID
    correlation_id: UUID
    artifact_origin: str | None = None
    summary_origin: str | None = None


# ---------------------------------------------------------------------------
# memory_objects: write + read shapes
# ---------------------------------------------------------------------------


class MemoryObjectIn(BaseModel):
    """Write-shape for memory_objects. Corresponds to an INSERT payload."""

    scope: ScopedIdentity
    provenance: Provenance
    memory_type: MemoryType
    entity_type: str | None = None
    entity_id: UUID | None = None
    thread_id: UUID | None = None
    title: str | None = None
    summary: str
    detail: dict = Field(default_factory=dict)
    confidence: float | None = None
    visibility_scope: VisibilityScope = "office"
    status: MemoryStatus | None = None

    # Linkage arrays
    linked_receipt_ids: list[UUID] = Field(default_factory=list)
    linked_approval_ids: list[UUID] = Field(default_factory=list)
    linked_artifact_ids: list[UUID] = Field(default_factory=list)
    linked_workflow_run_ids: list[UUID] = Field(default_factory=list)

    # Time model — caller supplies domain timestamps; DB sets created_at / last_activity_at
    event_at: datetime | None = None
    source_updated_at: datetime | None = None
    promoted_at: datetime | None = None
    approved_at: datetime | None = None
    executed_at: datetime | None = None
    summary_window_start_at: datetime | None = None
    summary_window_end_at: datetime | None = None
    fresh_until: datetime | None = None

    # Optional pre-computed embedding (1536 or 1536 dims depending on model config)
    # When None and embed=True on write, service computes it.
    embedding: list[float] | None = None

    # Idempotency key — NULL allowed for ephemeral objects; unique within tenant+suite
    idempotency_key: str | None = None

    @field_validator("summary")
    @classmethod
    def summary_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("summary must be a non-empty string")
        return v

    @field_validator("confidence")
    @classmethod
    def confidence_range(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0, 1]; got {v}")
        return v

    @field_validator("embedding")
    @classmethod
    def embedding_dims(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != 1536:
            raise ValueError(
                f"embedding must have exactly 1536 dimensions (text-embedding-3-large); got {len(v)}"
            )
        return v


class MemoryObjectOut(BaseModel):
    """Read-shape returned from DB after INSERT or SELECT."""

    memory_id: UUID
    scope: ScopedIdentity
    provenance: Provenance
    memory_type: MemoryType
    schema_version: str = "v1"
    entity_type: str | None = None
    entity_id: UUID | None = None
    thread_id: UUID | None = None
    title: str | None = None
    summary: str
    detail: dict = Field(default_factory=dict)
    confidence: float | None = None
    visibility_scope: VisibilityScope = "office"
    status: MemoryStatus | None = None

    linked_receipt_ids: list[UUID] = Field(default_factory=list)
    linked_approval_ids: list[UUID] = Field(default_factory=list)
    linked_artifact_ids: list[UUID] = Field(default_factory=list)
    linked_workflow_run_ids: list[UUID] = Field(default_factory=list)

    event_at: datetime | None = None
    created_at: datetime
    source_updated_at: datetime | None = None
    promoted_at: datetime | None = None
    approved_at: datetime | None = None
    executed_at: datetime | None = None
    last_activity_at: datetime
    summary_window_start_at: datetime | None = None
    summary_window_end_at: datetime | None = None
    fresh_until: datetime | None = None

    # 1536-dim vector; only populated when explicitly requested
    embedding: list[float] | None = None

    idempotency_key: str | None = None

    @field_validator("embedding")
    @classmethod
    def embedding_dims(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != 1536:
            raise ValueError(
                f"embedding must have exactly 1536 dimensions; got {len(v)}"
            )
        return v


# ---------------------------------------------------------------------------
# threads: write + read shapes
# ---------------------------------------------------------------------------


class ThreadIn(BaseModel):
    """Write-shape for threads."""

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    thread_type: ThreadType
    finance_thread_subtype: FinanceThreadSubtype | None = None
    canonical_entity_type: str | None = None
    canonical_entity_id: UUID | None = None
    title: str | None = None
    status: ThreadStatus = "open"
    first_event_at: datetime | None = None
    participants: list[UUID] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ThreadOut(BaseModel):
    """Read-shape returned from DB."""

    thread_id: UUID
    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    thread_type: ThreadType
    finance_thread_subtype: FinanceThreadSubtype | None = None
    canonical_entity_type: str | None = None
    canonical_entity_id: UUID | None = None
    title: str | None = None
    status: ThreadStatus
    first_event_at: datetime
    last_activity_at: datetime
    latest_memory_id: UUID | None = None
    latest_receipt_id: str | None = None
    latest_approval_id: str | None = None
    participants: list[UUID] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


# ---------------------------------------------------------------------------
# proactive_candidates: write + read shapes
# ---------------------------------------------------------------------------


class ProactiveCandidateIn(BaseModel):
    """Write-shape for proactive_candidates."""

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    owner_agent: SourceAgent
    source_event_ids: list[UUID] = Field(default_factory=list)
    source_memory_ids: list[UUID] = Field(default_factory=list)
    entity_type: str | None = None
    entity_id: UUID | None = None
    thread_id: UUID | None = None
    recommended_action: RecommendedAction
    action_class: ActionClass
    why_now: str
    confidence: float
    risk_tier: RiskTier
    needs_approval: bool = False
    receipt_required: bool = False
    due_at: datetime | None = None
    cooldown_until: datetime | None = None
    status: CandidateStatus = "open"

    @field_validator("confidence")
    @classmethod
    def confidence_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0, 1]; got {v}")
        return v


class ProactiveCandidateOut(BaseModel):
    """Read-shape for proactive_candidates."""

    candidate_id: UUID
    schema_version: str = "v1"
    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    owner_agent: SourceAgent
    source_event_ids: list[UUID] = Field(default_factory=list)
    source_memory_ids: list[UUID] = Field(default_factory=list)
    entity_type: str | None = None
    entity_id: UUID | None = None
    thread_id: UUID | None = None
    recommended_action: RecommendedAction
    action_class: ActionClass
    why_now: str
    confidence: float
    risk_tier: RiskTier
    needs_approval: bool
    receipt_required: bool
    due_at: datetime | None = None
    cooldown_until: datetime | None = None
    status: CandidateStatus
    created_at: datetime
    last_activity_at: datetime


# ---------------------------------------------------------------------------
# approval_links: write + read shapes
# NOTE: approval_id is str (TEXT PK from approval_requests)
# ---------------------------------------------------------------------------


class ApprovalLinkIn(BaseModel):
    """Write-shape for approval_links."""

    tenant_id: UUID
    suite_id: UUID
    approval_id: str  # TEXT PK from approval_requests
    linked_candidate_id: UUID | None = None
    linked_memory_ids: list[UUID] = Field(default_factory=list)
    linked_workflow_run_id: UUID | None = None
    requested_by_agent: SourceAgent
    approval_status: ApprovalStatus = "pending"
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    approver_actor_id: UUID | None = None
    reason: str | None = None


class ApprovalLinkOut(BaseModel):
    """Read-shape for approval_links."""

    approval_link_id: UUID
    tenant_id: UUID
    suite_id: UUID
    approval_id: str  # TEXT PK
    linked_candidate_id: UUID | None = None
    linked_memory_ids: list[UUID] = Field(default_factory=list)
    linked_workflow_run_id: UUID | None = None
    requested_by_agent: SourceAgent
    approval_status: ApprovalStatus
    requested_at: datetime
    decided_at: datetime | None = None
    approver_actor_id: UUID | None = None
    reason: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# receipt_memory_links: write + read shapes
# NOTE: receipt_id is str (TEXT PK from receipts)
# ---------------------------------------------------------------------------


class ReceiptMemoryLinkIn(BaseModel):
    """Write-shape for receipt_memory_links (append-only)."""

    receipt_id: str  # TEXT PK from receipts
    memory_id: UUID
    linked_via: str | None = None
    tenant_id: UUID
    suite_id: UUID


class ReceiptMemoryLinkOut(BaseModel):
    """Read-shape for receipt_memory_links."""

    receipt_id: str  # TEXT PK
    memory_id: UUID
    linked_via: str | None = None
    tenant_id: UUID
    suite_id: UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# memory_event_inbox: write shape (MemoryEventEnvelope)
# ---------------------------------------------------------------------------


class MemoryEventEnvelope(BaseModel):
    """Append-only intake event. Maps to memory_event_inbox table.

    idempotency_key is NOT NULL in the DB (unique per tenant+suite) — required here.
    """

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    actor_id: UUID | None = None
    user_id: UUID | None = None
    event_type: str
    source_surface: SourceSurface | None = None
    source_agent: SourceAgent | None = None
    runtime_family: RuntimeFamily | None = None
    channel: Channel | None = None
    trace_id: UUID
    correlation_id: UUID
    source_record_id: str | None = None
    session_id: UUID | None = None
    thread_id: UUID | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    payload: dict = Field(default_factory=dict)
    risk_tier: RiskTier = "yellow"
    needs_approval: bool = False
    receipt_required: bool = False
    event_at: datetime
    source_updated_at: datetime | None = None
    idempotency_key: str

    @field_validator("idempotency_key")
    @classmethod
    def idempotency_key_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("idempotency_key must be a non-empty string (NOT NULL in DB)")
        return v


# ---------------------------------------------------------------------------
# Pass 3 — Refinery + candidate engine + brief materializer shapes
# ---------------------------------------------------------------------------


class RefineResult(BaseModel):
    """Outcome of TranscriptEventRefinery.refine().

    Lists every memory_object and proactive_candidate produced for a single
    inbox event. Used by the Temporal activity to emit per-event metrics and
    by tests to assert refinery behavior.
    """

    memory_ids: list[UUID] = Field(default_factory=list)
    candidate_ids: list[UUID] = Field(default_factory=list)


class CandidateQuery(BaseModel):
    """Query envelope for ProactiveCandidateEngine.query().

    All scope fields required (Law #6). Other fields filter the result set.
    """

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    owner_agent: list[SourceAgent] | None = None
    status: list[CandidateStatus] | None = None
    due_before: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)


# ---------------------------------------------------------------------------
# Pass 5 — Memory search request / response shapes
# ---------------------------------------------------------------------------


class MemorySearchRequest(BaseModel):
    """Hybrid memory search request envelope (§3.4 ranking pipeline).

    Caller MUST supply at least one of: query_text, query_embedding,
    (entity_type + entity_id), or thread_id. Empty searches return an empty
    result rather than scanning the entire memory_objects table.

    Tenant isolation (Law #6): tenant_id / suite_id / office_id are required
    and validated against the request scope at the service boundary.
    """

    # --- scope (required, Law #6) ---
    tenant_id: UUID
    suite_id: UUID
    office_id: UUID

    # --- query inputs (at least one of text / embedding / entity / thread) ---
    query_text: str | None = None
    query_embedding: list[float] | None = None

    # --- anchors (Tier 1 / Tier 2) ---
    entity_type: str | None = None
    entity_id: UUID | None = None
    thread_id: UUID | None = None

    # --- filters ---
    memory_types: list[MemoryType] | None = None
    visibility_scope: VisibilityScope = "office"
    tags: list[str] | None = None
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    min_confidence: float | None = None

    # --- result shaping ---
    include_raw: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None

    @field_validator("query_embedding")
    @classmethod
    def query_embedding_dims(cls, v: list[float] | None) -> list[float] | None:
        """Embedding length must match memory_objects.embedding column (vector(1536))."""
        if v is not None and len(v) != 1536:
            raise ValueError(
                f"query_embedding must have exactly 1536 dimensions; got {len(v)}"
            )
        return v

    @field_validator("min_confidence")
    @classmethod
    def min_confidence_range(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"min_confidence must be in [0, 1]; got {v}")
        return v


class MemorySearchResponse(BaseModel):
    """Hybrid memory search response. items are ordered by score DESC, then
    last_activity_at DESC. total may be None when an exact count is too
    expensive to compute on the request path.
    """

    items: list[MemoryObjectOut]
    total: int | None = None
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Brief output shapes — returned by BriefMaterializer + GET /v1/briefs/* routes
# ---------------------------------------------------------------------------


class OfficeBriefOut(BaseModel):
    """Read-shape for office_brief_cache rows.

    Mirrors migration 098 office_brief_cache columns. brief_json carries the
    structured projection (recent_memory, open_candidates, pending_approvals,
    recent_receipts) so the UI can render without secondary fetches.
    """

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    brief_text: str | None = None
    brief_json: dict = Field(default_factory=dict)
    due_now_count: int = 0
    overdue_count: int = 0
    pending_approval_count: int = 0
    recent_receipts_count: int = 0
    last_built_at: datetime
    freshness_seq: int = 0


class FinanceBriefOut(BaseModel):
    """Read-shape for finance_brief_cache rows."""

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    brief_text: str | None = None
    brief_json: dict = Field(default_factory=dict)
    due_now_count: int = 0
    overdue_count: int = 0
    pending_approval_count: int = 0
    recent_receipts_count: int = 0
    provider_health: dict = Field(default_factory=dict)
    aging_summary: dict = Field(default_factory=dict)
    cash_narrative: str | None = None
    last_built_at: datetime
    freshness_seq: int = 0


class ServiceBriefOut(BaseModel):
    """Read-shape for service_brief_cache rows (Wave 5.1b-4).

    Mirrors office_brief_cache layout (migration 101). Service-specific
    summary counters are extracted from brief_json by BriefMaterializer on
    every build so the cache table schema stays stable across feature passes.

    Fields:
      recent_picks_count      — last 5 material_pick decision_facts (Drew)
      recent_overrides_count  — last 3 material_override decision_facts
      open_pending_intents_count — unresolved service-scope pending_intents
      recent_handoffs_count   — last 3 handoff_note entries (visibility=service)
      active_threads_count    — project/job/property threads with recent activity
    """

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    brief_text: str | None = None
    brief_json: dict = Field(default_factory=dict)
    due_now_count: int = 0
    overdue_count: int = 0
    pending_approval_count: int = 0
    recent_receipts_count: int = 0
    # Service-specific summary counters (derived from brief_json on each build)
    recent_picks_count: int = 0
    recent_overrides_count: int = 0
    open_pending_intents_count: int = 0
    recent_handoffs_count: int = 0
    active_threads_count: int = 0
    last_built_at: datetime
    freshness_seq: int = 0


class ThreadBriefOut(BaseModel):
    """Read-shape for thread_brief_cache rows."""

    thread_id: UUID
    tenant_id: UUID
    suite_id: UUID
    summary: str | None = None
    last_promise: str | None = None
    pending_blockers: list[dict] = Field(default_factory=list)
    latest_receipt_id: str | None = None
    next_best_action: dict = Field(default_factory=dict)
    last_built_at: datetime
    freshness_seq: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Literal types
    "MemoryType",
    "MemoryStatus",
    "RuntimeFamily",
    "Channel",
    "SourceSurface",
    "SourceAgent",
    "VisibilityScope",
    "RiskTier",
    "ThreadType",
    "FinanceThreadSubtype",
    "RecommendedAction",
    "ActionClass",
    "CandidateStatus",
    "ApprovalStatus",
    "EventInboxStatus",
    "ThreadStatus",
    # Composite value objects
    "ScopedIdentity",
    "Provenance",
    # memory_objects
    "MemoryObjectIn",
    "MemoryObjectOut",
    # threads
    "ThreadIn",
    "ThreadOut",
    # proactive_candidates
    "ProactiveCandidateIn",
    "ProactiveCandidateOut",
    # approval_links
    "ApprovalLinkIn",
    "ApprovalLinkOut",
    # receipt_memory_links
    "ReceiptMemoryLinkIn",
    "ReceiptMemoryLinkOut",
    # memory_event_inbox
    "MemoryEventEnvelope",
    # Pass 3 — refinery + candidate query + brief outs
    "RefineResult",
    "CandidateQuery",
    "OfficeBriefOut",
    "FinanceBriefOut",
    "ServiceBriefOut",
    "ThreadBriefOut",
    # Pass 5 — memory search
    "MemorySearchRequest",
    "MemorySearchResponse",
]
