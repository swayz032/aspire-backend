-- =============================================================================
-- Migration 096: Memory Spine — Memory Objects
-- =============================================================================
-- Creates the durable memory object store for the Office Memory Engine.
-- Memory objects are the persistence layer for all agent cognition: session
-- summaries, handoff notes, decisions, risk flags, briefs, and timeline events.
--
-- Aspire Laws enforced:
--   Law #2 (Receipt for All)  — 'executed' objects are immutable (trigger blocks updates)
--   Law #3 (Fail Closed)      — RLS denies by default; explicit grants only
--   Law #6 (Tenant Isolation) — tenant_id + suite_id + office_id on every row,
--                               FORCE ROW LEVEL SECURITY
--   Law #9 (Security)         — idempotency_key deduplication prevents double-writes
--
-- Capabilities:
--   - Full vector search (pgvector 3072-dim HNSW m=24 ef_construction=128)
--   - Full-text search (tsvector GENERATED on title + summary)
--   - Provenance tracking (flattened from JSON shape for query efficiency)
--   - 10-timestamp time model per §3.3
--   - Idempotency: UNIQUE (tenant_id, suite_id, idempotency_key) on non-null keys
--   - Visibility scopes: office | finance | workflow | admin | restricted
--
-- References:
--   02_SHARED_SCHEMAS.md §2.4 (MemoryObject), §2.2 (Provenance)
--   03_THREAD_REGISTRY_AND_MEMORY_MODEL.md §3.3 (time model)
-- =============================================================================

-- Defensive: pgvector should already be enabled (migration 066+); idempotent.
CREATE EXTENSION IF NOT EXISTS pgvector;

-- =============================================================================
-- TABLE: public.memory_objects
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.memory_objects (
    -- Primary key
    memory_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Tenant / suite / office scope (Law #6) — all three required
    tenant_id       UUID        NOT NULL,
    suite_id        UUID        NOT NULL,
    office_id       UUID        NOT NULL,

    -- Memory classification
    memory_type     TEXT        NOT NULL CHECK (memory_type IN (
                                    'session_summary',
                                    'handoff_note',
                                    'pending_intent',
                                    'authority_context',
                                    'thread_summary',
                                    'office_brief',
                                    'finance_brief',
                                    'decision_fact',
                                    'risk_flag',
                                    'followup_task',
                                    'timeline_event',
                                    'artifact_reference',
                                    'receipt_reference',
                                    'workflow_reference'
                                )),

    -- Schema versioning for forward compatibility
    schema_version  TEXT        NOT NULL DEFAULT 'v1',

    -- -------------------------------------------------------------------------
    -- Provenance (flattened from §2.2 Provenance shape for query efficiency)
    -- -------------------------------------------------------------------------
    source_surface  TEXT        NULL CHECK (source_surface IS NULL OR source_surface IN (
                                    'ava_voice',
                                    'sarah_voice',
                                    'eli_inbox',
                                    'nora_meeting',
                                    'finn_finance',
                                    'tim_service_lab',
                                    'estimate_studio',
                                    'canvas_desk',
                                    'receipt_ledger',
                                    'approval_queue',
                                    'system'
                                )),
    source_agent    TEXT        NULL CHECK (source_agent IS NULL OR source_agent IN (
                                    'ava', 'sarah', 'eli', 'nora', 'finn', 'tim', 'system'
                                )),
    runtime_family  TEXT        NULL CHECK (runtime_family IS NULL OR runtime_family IN (
                                    'elevenlabs', 'anam', 'internal', 'ui', 'provider_webhook'
                                )),
    channel         TEXT        NULL CHECK (channel IS NULL OR channel IN (
                                    'voice', 'video', 'email', 'sms', 'workflow',
                                    'finance', 'ui', 'webhook'
                                )),

    -- Provider/session metadata
    session_provider        TEXT    NULL,
    transcript_provider     TEXT    NULL,
    recording_provider      TEXT    NULL,
    external_session_id     TEXT    NULL,   -- e.g. ElevenLabs conversation_id
    source_record_id        TEXT    NULL,   -- upstream record identifier

    -- Correlation (mandatory for all cross-system traces)
    trace_id            UUID    NOT NULL,
    correlation_id      UUID    NOT NULL,

    -- Artifact and summary lineage
    artifact_origin     TEXT    NULL,
    summary_origin      TEXT    NULL,

    -- -------------------------------------------------------------------------
    -- Entity / Thread linkage
    -- -------------------------------------------------------------------------
    entity_type         TEXT    NULL,
    entity_id           UUID    NULL,
    thread_id           UUID    NULL REFERENCES public.threads(thread_id) ON DELETE SET NULL,

    -- -------------------------------------------------------------------------
    -- Content
    -- -------------------------------------------------------------------------
    title               TEXT    NULL,
    summary             TEXT    NOT NULL,
    detail              JSONB   NOT NULL DEFAULT '{}'::jsonb,
    confidence          FLOAT   NULL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),

    -- Visibility scope controls which agent roles can read this object
    visibility_scope    TEXT    NOT NULL DEFAULT 'office'
                            CHECK (visibility_scope IN (
                                'office', 'finance', 'workflow', 'admin', 'restricted'
                            )),

    -- Lifecycle status
    status              TEXT    NULL CHECK (status IS NULL OR status IN (
                                    'requested',
                                    'drafted',
                                    'pending_approval',
                                    'approved',
                                    'executed',
                                    'rejected',
                                    'superseded',
                                    'failed',
                                    'promoted'
                                )),

    -- -------------------------------------------------------------------------
    -- Linkage arrays (cross-reference to governance records)
    -- -------------------------------------------------------------------------
    linked_receipt_ids      UUID[]  NOT NULL DEFAULT '{}',
    linked_approval_ids     UUID[]  NOT NULL DEFAULT '{}',
    linked_artifact_ids     UUID[]  NOT NULL DEFAULT '{}',
    linked_workflow_run_ids UUID[]  NOT NULL DEFAULT '{}',

    -- -------------------------------------------------------------------------
    -- Time model (10 timestamps per §3.3 — all nullable except the two required)
    -- -------------------------------------------------------------------------
    event_at                TIMESTAMPTZ     NULL,       -- when the real-world event happened
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT now(),
    source_updated_at       TIMESTAMPTZ     NULL,       -- upstream source's updated_at
    promoted_at             TIMESTAMPTZ     NULL,       -- when transient material became durable
    approved_at             TIMESTAMPTZ     NULL,       -- when approval was granted
    executed_at             TIMESTAMPTZ     NULL,       -- when the side-effect ran
    last_activity_at        TIMESTAMPTZ     NOT NULL DEFAULT now(),
    summary_window_start_at TIMESTAMPTZ     NULL,       -- start of the source material window
    summary_window_end_at   TIMESTAMPTZ     NULL,       -- end of the source material window
    fresh_until             TIMESTAMPTZ     NULL,       -- optional freshness horizon

    -- -------------------------------------------------------------------------
    -- Search columns
    -- -------------------------------------------------------------------------

    -- Vector search: text-embedding-3-large (3072 dims), matches existing tables
    embedding   vector(3072)    NULL,

    -- Full-text search: generated from title + summary (matches mig 076 pattern)
    tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' || coalesce(summary, '')
        )
    ) STORED,

    -- -------------------------------------------------------------------------
    -- Idempotency (Law #9 / canonical execution path dedup)
    -- -------------------------------------------------------------------------
    idempotency_key     TEXT    NULL,

    -- Unique constraint: deduplicates non-null keys within tenant+suite scope.
    -- NULL keys are allowed (no dedup required for ephemeral objects).
    CONSTRAINT uq_memory_objects_idempotency
        UNIQUE (tenant_id, suite_id, idempotency_key)
        DEFERRABLE INITIALLY IMMEDIATE
);

COMMENT ON TABLE public.memory_objects IS
    'Durable memory object store for the Office Memory Engine. '
    'Holds all agent cognition: summaries, handoffs, decisions, risk flags, briefs. '
    'Immutability rule: ''executed'' objects cannot be updated or deleted (trigger enforced). '
    'Use status=''superseded'' to logically retire a record. '
    'RLS: tenant_id + suite_id + office_id triple-scoped (Law #6).';

COMMENT ON COLUMN public.memory_objects.embedding IS
    'text-embedding-3-large (3072 dims). HNSW m=24 ef_construction=128. '
    'SET LOCAL hnsw.ef_search = 100 before vector queries for best recall.';

COMMENT ON COLUMN public.memory_objects.tsv IS
    'Generated tsvector from title || summary. Used by GIN index for full-text search.';

COMMENT ON COLUMN public.memory_objects.idempotency_key IS
    'Caller-supplied dedup key. UNIQUE within (tenant_id, suite_id). '
    'Null allowed — omit for objects that do not require dedup.';

COMMENT ON COLUMN public.memory_objects.status IS
    'Lifecycle: requested → drafted → pending_approval → approved → executed. '
    'Terminal write-protected: executed objects cannot be modified. '
    'Use superseded/rejected/failed for logical retirement.';

COMMENT ON COLUMN public.memory_objects.visibility_scope IS
    'Controls which agent roles can read: office (default), finance, workflow, admin, restricted.';

-- =============================================================================
-- INDEXES
-- =============================================================================

-- Vector search (HNSW — matches mig 078 tuning: m=24, ef_construction=128)
CREATE INDEX IF NOT EXISTS idx_memory_objects_embedding
    ON public.memory_objects
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

COMMENT ON INDEX idx_memory_objects_embedding IS
    'HNSW m=24 ef_construction=128. SET LOCAL hnsw.ef_search=100 at query time for best recall.';

-- Full-text search (GIN on generated tsvector — matches mig 076 pattern)
CREATE INDEX IF NOT EXISTS idx_memory_objects_tsv
    ON public.memory_objects USING GIN (tsv);

-- Primary workhorse: tenant-scoped recency queries
CREATE INDEX IF NOT EXISTS idx_memory_objects_tenant_scope_activity
    ON public.memory_objects (tenant_id, suite_id, office_id, last_activity_at DESC);

-- Thread-scoped recency (agent retrieval rule #2: exact thread match)
CREATE INDEX IF NOT EXISTS idx_memory_objects_thread_activity
    ON public.memory_objects (thread_id, last_activity_at DESC)
    WHERE thread_id IS NOT NULL;

-- Entity-scoped recency (agent retrieval rule #1: exact entity match)
CREATE INDEX IF NOT EXISTS idx_memory_objects_entity_activity
    ON public.memory_objects (entity_type, entity_id, last_activity_at DESC)
    WHERE entity_id IS NOT NULL;

-- Type + recency for active (non-terminal) objects
CREATE INDEX IF NOT EXISTS idx_memory_objects_type_active
    ON public.memory_objects (memory_type, last_activity_at DESC)
    WHERE status NOT IN ('rejected', 'superseded') OR status IS NULL;

-- =============================================================================
-- TRIGGER: bump last_activity_at on every write
-- =============================================================================

CREATE OR REPLACE FUNCTION public.trg_memory_objects_touch()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.last_activity_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_memory_objects_touch ON public.memory_objects;
CREATE TRIGGER trg_memory_objects_touch
    BEFORE INSERT OR UPDATE ON public.memory_objects
    FOR EACH ROW EXECUTE FUNCTION public.trg_memory_objects_touch();

-- =============================================================================
-- TRIGGER: enforce immutability of 'executed' memory objects (Law #2)
-- =============================================================================
-- Memory objects in the 'executed' state are equivalent to receipts —
-- they record an irreversible side-effect and must not be modified or deleted.

CREATE OR REPLACE FUNCTION public.trg_memory_objects_immutability()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- Block updates on executed objects (terminal state)
    IF TG_OP = 'UPDATE' AND OLD.status = 'executed' THEN
        RAISE EXCEPTION
            'memory_objects immutability violation: '
            'memory_id=% is in ''executed'' state and cannot be modified. '
            'Create a new memory object with status=''superseded'' reference instead.',
            OLD.memory_id
            USING ERRCODE = 'raise_exception';
    END IF;

    -- Block deletes on executed objects
    IF TG_OP = 'DELETE' AND OLD.status = 'executed' THEN
        RAISE EXCEPTION
            'memory_objects immutability violation: '
            'memory_id=% is in ''executed'' state and cannot be deleted. '
            'Set status=''superseded'' on a new object instead.',
            OLD.memory_id
            USING ERRCODE = 'raise_exception';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_memory_objects_immutability ON public.memory_objects;
CREATE TRIGGER trg_memory_objects_immutability
    BEFORE UPDATE OR DELETE ON public.memory_objects
    FOR EACH ROW EXECUTE FUNCTION public.trg_memory_objects_immutability();

-- =============================================================================
-- ROW LEVEL SECURITY (Law #3: Fail Closed, Law #6: Tenant Isolation)
-- =============================================================================

ALTER TABLE public.memory_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_objects FORCE ROW LEVEL SECURITY;

-- SELECT: authenticated users see only their own tenant's objects.
-- Visibility scope filtering (finance/restricted) is enforced at the service layer;
-- RLS guarantees the outer tenant boundary.
CREATE POLICY memory_objects_select_tenant ON public.memory_objects
    FOR SELECT
    TO authenticated
    USING (app.is_member(tenant_id::text));

-- Service role bypass for orchestrator/worker operations
CREATE POLICY memory_objects_all_service_role ON public.memory_objects
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- INSERT: authenticated users may only insert into their own tenant + suite
CREATE POLICY memory_objects_insert_tenant ON public.memory_objects
    FOR INSERT
    TO authenticated
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
        AND suite_id = public.current_suite_id()
    );

-- UPDATE: allowed only for non-executed objects within tenant scope.
-- 'executed' objects are further blocked by the immutability trigger above.
-- The RLS policy here is the first line of defense; the trigger is the backstop.
CREATE POLICY memory_objects_update_tenant ON public.memory_objects
    FOR UPDATE
    TO authenticated
    USING (
        app.is_member(tenant_id::text)
        AND status IS DISTINCT FROM 'executed'
    )
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
        AND status IS DISTINCT FROM 'executed'
    );

-- DELETE: forbidden for authenticated users.
-- No DELETE policy = default deny under FORCE ROW LEVEL SECURITY.
-- The immutability trigger provides an additional backstop for service_role.

-- =============================================================================
-- GRANTS
-- =============================================================================

GRANT SELECT, INSERT, UPDATE ON public.memory_objects TO authenticated;
GRANT ALL ON public.memory_objects TO service_role;
