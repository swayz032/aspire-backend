-- =============================================================================
-- Migration 101: Wave 5.1b-2 — Service Brief Cache
-- =============================================================================
-- Creates the Service Hub operational memory brief cache, mirroring the
-- office_brief_cache shape exactly (migration 098). Consumed by the Service
-- Hub agent surface (Tim, Drew, dispatch, scheduling, jobs, inspections,
-- estimate studio) and rendered into the Service Memory page.
--
-- This migration ALSO extends the memory_objects.visibility_scope CHECK
-- constraint (mig 096) to include 'service' — the schema layer (Wave 5.1b-1,
-- commit e583c37) already added 'service' to VisibilityScope at
-- orchestrator/src/aspire_orchestrator/schemas/memory_v1.py:135, but the DB
-- CHECK still enumerates only office/finance/workflow/admin/restricted.
--
-- Aspire Laws enforced:
--   Law #2 (Receipt for All)  — BriefMaterializer emits receipts on each
--                               refresh (Wave 5.1b-4); this migration only
--                               stores caches.
--   Law #3 (Fail Closed)      — RLS denies by default; explicit grants only
--   Law #6 (Tenant Isolation) — tenant_id + suite_id + office_id on every row,
--                               FORCE ROW LEVEL SECURITY
--
-- References:
--   098_brief_caches.sql       (mirrored shape — office_brief_cache)
--   096_memory_objects.sql     (visibility_scope CHECK source)
--   memory_v1.py:135           (VisibilityScope schema with 'service')
-- =============================================================================

-- =============================================================================
-- TABLE: public.service_brief_cache
-- =============================================================================
-- One row per (tenant_id, suite_id, office_id). Built from recent memory
-- objects with visibility_scope='service', open service-hub candidates,
-- pending approvals on service work, and recent service receipts.
-- Shape mirrors office_brief_cache exactly (no service-specific columns yet —
-- can be added in a future migration when needs are concrete).

CREATE TABLE IF NOT EXISTS public.service_brief_cache (
    -- Composite primary key — one cache row per office
    tenant_id       UUID    NOT NULL,
    suite_id        UUID    NOT NULL,
    office_id       UUID    NOT NULL,

    -- Rendered brief (human-readable Markdown / paragraph form)
    brief_text      TEXT    NULL,

    -- Structured brief (per §3.5 — JSON projection of the brief contents)
    brief_json      JSONB   NOT NULL DEFAULT '{}'::jsonb,

    -- Roll-up counters used by the Service Hub agent UI
    due_now_count           INT     NOT NULL DEFAULT 0,
    overdue_count           INT     NOT NULL DEFAULT 0,
    pending_approval_count  INT     NOT NULL DEFAULT 0,
    recent_receipts_count   INT     NOT NULL DEFAULT 0,

    -- Refresh tracking
    last_built_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    freshness_seq   BIGINT  NOT NULL DEFAULT 0,

    PRIMARY KEY (tenant_id, suite_id, office_id)
);

COMMENT ON TABLE public.service_brief_cache IS
    'Per-office Service Hub brief snapshot (visibility_scope=service). '
    'Refreshed by BriefMaterializer (Temporal 60s sweep + on-demand, Wave 5.1b-4). '
    'freshness_seq is monotonic for race detection. '
    'RLS: tenant_id + suite_id + office_id (Law #6).';

COMMENT ON COLUMN public.service_brief_cache.freshness_seq IS
    'Monotonic counter incremented on every refresh. '
    'Readers use this to detect concurrent rebuilds and race conditions.';

-- Staleness scan (BriefMaterializer 60s sweep)
CREATE INDEX IF NOT EXISTS idx_service_brief_cache_staleness
    ON public.service_brief_cache (tenant_id, suite_id, last_built_at DESC);

-- RLS
ALTER TABLE public.service_brief_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.service_brief_cache FORCE ROW LEVEL SECURITY;

CREATE POLICY service_brief_cache_select_tenant ON public.service_brief_cache
    FOR SELECT TO authenticated
    USING (app.is_member(tenant_id::text));

CREATE POLICY service_brief_cache_all_service_role ON public.service_brief_cache
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

CREATE POLICY service_brief_cache_insert_tenant ON public.service_brief_cache
    FOR INSERT TO authenticated
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
        AND suite_id = public.current_suite_id()
    );

CREATE POLICY service_brief_cache_update_tenant ON public.service_brief_cache
    FOR UPDATE TO authenticated
    USING (app.is_member(tenant_id::text))
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

GRANT SELECT, INSERT, UPDATE ON public.service_brief_cache TO authenticated;
GRANT ALL ON public.service_brief_cache TO service_role;


-- =============================================================================
-- EXTEND: memory_objects.visibility_scope CHECK constraint
-- =============================================================================
-- The original mig 096 CHECK enumerates:
--   'office', 'finance', 'workflow', 'admin', 'restricted'
-- Wave 5.1b adds 'service' for Service Hub operational memory (Drew, Tim,
-- dispatch, scheduling, jobs, inspections, estimate studio).
--
-- Postgres named the inline CHECK constraint
-- `memory_objects_visibility_scope_check`. We drop+recreate it. This is a pure
-- relaxation (adds a new allowed value), so no existing row can violate the
-- new constraint.

ALTER TABLE public.memory_objects
    DROP CONSTRAINT IF EXISTS memory_objects_visibility_scope_check;

ALTER TABLE public.memory_objects
    ADD CONSTRAINT memory_objects_visibility_scope_check
    CHECK (visibility_scope IN (
        'office', 'finance', 'service', 'workflow', 'admin', 'restricted'
    ));

COMMENT ON COLUMN public.memory_objects.visibility_scope IS
    'Controls which agent roles can read: office (default), finance, service, workflow, admin, restricted.';


-- =============================================================================
-- DOWN (commented; do not run automatically)
-- =============================================================================
-- ALTER TABLE public.memory_objects
--     DROP CONSTRAINT IF EXISTS memory_objects_visibility_scope_check;
-- ALTER TABLE public.memory_objects
--     ADD CONSTRAINT memory_objects_visibility_scope_check
--     CHECK (visibility_scope IN ('office', 'finance', 'workflow', 'admin', 'restricted'));
--
-- DROP POLICY IF EXISTS service_brief_cache_update_tenant ON public.service_brief_cache;
-- DROP POLICY IF EXISTS service_brief_cache_insert_tenant ON public.service_brief_cache;
-- DROP POLICY IF EXISTS service_brief_cache_all_service_role ON public.service_brief_cache;
-- DROP POLICY IF EXISTS service_brief_cache_select_tenant ON public.service_brief_cache;
-- DROP INDEX IF EXISTS public.idx_service_brief_cache_staleness;
-- DROP TABLE IF EXISTS public.service_brief_cache;
