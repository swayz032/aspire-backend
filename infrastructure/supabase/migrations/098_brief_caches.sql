-- =============================================================================
-- Migration 098: Memory Spine — Brief Caches
-- =============================================================================
-- Creates three brief cache tables consumed by the agent surface (Memory Engine
-- page, Finance Memory page, voice/video session brokers):
--
--   1. public.office_brief_cache  — per (tenant, suite, office) office summary
--   2. public.finance_brief_cache — per (tenant, suite, office) finance summary
--   3. public.thread_brief_cache  — per thread_id rolled-up summary
--
-- Each cache row carries:
--   - last_built_at  : when the row was last refreshed
--   - freshness_seq  : monotonic counter incremented on every refresh
--                      (used by readers to detect race conditions)
--
-- These are plain tables (NOT materialized views) so the BriefMaterializer
-- can refresh selectively (60s sweep + on-demand) without locking large
-- views. Each refresh UPSERTs into the cache and bumps freshness_seq.
--
-- Aspire Laws enforced:
--   Law #2 (Receipt for All)  — the BriefMaterializer service emits receipts
--                               on each refresh; this migration only stores caches.
--   Law #3 (Fail Closed)      — RLS denies by default; explicit grants only
--   Law #6 (Tenant Isolation) — tenant_id + suite_id + office_id on every row,
--                               FORCE ROW LEVEL SECURITY
--
-- References:
--   the-image-was-off-calm-lynx.md §3.5 (brief caches), §4 (BriefMaterializer)
--   095_memory_spine_threads.sql (threads FK target)
--   097_memory_spine_links.sql (RLS pattern reference)
-- =============================================================================

-- =============================================================================
-- TABLE: public.office_brief_cache
-- =============================================================================
-- One row per (tenant_id, suite_id, office_id). Built from recent memory
-- objects with visibility_scope='office', open proactive_candidates, pending
-- approval_links, and recent receipts.

CREATE TABLE IF NOT EXISTS public.office_brief_cache (
    -- Composite primary key — one cache row per office
    tenant_id       UUID    NOT NULL,
    suite_id        UUID    NOT NULL,
    office_id       UUID    NOT NULL,

    -- Rendered brief (human-readable Markdown / paragraph form)
    brief_text      TEXT    NULL,

    -- Structured brief (per §3.5 — JSON projection of the brief contents)
    brief_json      JSONB   NOT NULL DEFAULT '{}'::jsonb,

    -- Roll-up counters used by the agent UI
    due_now_count           INT     NOT NULL DEFAULT 0,
    overdue_count           INT     NOT NULL DEFAULT 0,
    pending_approval_count  INT     NOT NULL DEFAULT 0,
    recent_receipts_count   INT     NOT NULL DEFAULT 0,

    -- Refresh tracking
    last_built_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    freshness_seq   BIGINT  NOT NULL DEFAULT 0,

    PRIMARY KEY (tenant_id, suite_id, office_id)
);

COMMENT ON TABLE public.office_brief_cache IS
    'Per-office brief snapshot. Refreshed by BriefMaterializer (Temporal 60s sweep + on-demand). '
    'freshness_seq is monotonic for race detection. RLS: tenant_id + suite_id + office_id (Law #6).';

COMMENT ON COLUMN public.office_brief_cache.freshness_seq IS
    'Monotonic counter incremented on every refresh. '
    'Readers use this to detect concurrent rebuilds and race conditions.';

-- Staleness scan (BriefMaterializer 60s sweep)
CREATE INDEX IF NOT EXISTS idx_office_brief_cache_staleness
    ON public.office_brief_cache (tenant_id, suite_id, last_built_at DESC);

-- RLS
ALTER TABLE public.office_brief_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.office_brief_cache FORCE ROW LEVEL SECURITY;

CREATE POLICY office_brief_cache_select_tenant ON public.office_brief_cache
    FOR SELECT TO authenticated
    USING (app.is_member(tenant_id::text));

CREATE POLICY office_brief_cache_all_service_role ON public.office_brief_cache
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

CREATE POLICY office_brief_cache_insert_tenant ON public.office_brief_cache
    FOR INSERT TO authenticated
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
        AND suite_id = public.current_suite_id()
    );

CREATE POLICY office_brief_cache_update_tenant ON public.office_brief_cache
    FOR UPDATE TO authenticated
    USING (app.is_member(tenant_id::text))
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

GRANT SELECT, INSERT, UPDATE ON public.office_brief_cache TO authenticated;
GRANT ALL ON public.office_brief_cache TO service_role;


-- =============================================================================
-- TABLE: public.finance_brief_cache
-- =============================================================================
-- Mirrors office_brief_cache shape but tracks finance-scoped state. Plus three
-- finance-specific columns: provider_health, aging_summary, cash_narrative.

CREATE TABLE IF NOT EXISTS public.finance_brief_cache (
    tenant_id       UUID    NOT NULL,
    suite_id        UUID    NOT NULL,
    office_id       UUID    NOT NULL,

    brief_text      TEXT    NULL,
    brief_json      JSONB   NOT NULL DEFAULT '{}'::jsonb,

    due_now_count           INT     NOT NULL DEFAULT 0,
    overdue_count           INT     NOT NULL DEFAULT 0,
    pending_approval_count  INT     NOT NULL DEFAULT 0,
    recent_receipts_count   INT     NOT NULL DEFAULT 0,

    -- Finance-specific roll-ups
    provider_health JSONB   NOT NULL DEFAULT '{}'::jsonb,
    aging_summary   JSONB   NOT NULL DEFAULT '{}'::jsonb,
    cash_narrative  TEXT    NULL,

    last_built_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    freshness_seq   BIGINT  NOT NULL DEFAULT 0,

    PRIMARY KEY (tenant_id, suite_id, office_id)
);

COMMENT ON TABLE public.finance_brief_cache IS
    'Per-office finance brief snapshot (visibility_scope=finance). '
    'Includes provider_health, aging_summary, cash_narrative beyond office shape. '
    'RLS: tenant_id + suite_id + office_id (Law #6).';

COMMENT ON COLUMN public.finance_brief_cache.provider_health IS
    'Snapshot of finance provider connection state (e.g., {"plaid":"healthy","stripe":"degraded"}).';

COMMENT ON COLUMN public.finance_brief_cache.aging_summary IS
    'Aggregate AR aging buckets (0-30, 31-60, 61-90, 90+).';

CREATE INDEX IF NOT EXISTS idx_finance_brief_cache_staleness
    ON public.finance_brief_cache (tenant_id, suite_id, last_built_at DESC);

-- RLS
ALTER TABLE public.finance_brief_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finance_brief_cache FORCE ROW LEVEL SECURITY;

CREATE POLICY finance_brief_cache_select_tenant ON public.finance_brief_cache
    FOR SELECT TO authenticated
    USING (app.is_member(tenant_id::text));

CREATE POLICY finance_brief_cache_all_service_role ON public.finance_brief_cache
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

CREATE POLICY finance_brief_cache_insert_tenant ON public.finance_brief_cache
    FOR INSERT TO authenticated
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
        AND suite_id = public.current_suite_id()
    );

CREATE POLICY finance_brief_cache_update_tenant ON public.finance_brief_cache
    FOR UPDATE TO authenticated
    USING (app.is_member(tenant_id::text))
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

GRANT SELECT, INSERT, UPDATE ON public.finance_brief_cache TO authenticated;
GRANT ALL ON public.finance_brief_cache TO service_role;


-- =============================================================================
-- TABLE: public.thread_brief_cache
-- =============================================================================
-- One row per thread_id. Built from recent memory in the thread, the latest
-- pending_intent (last_promise), open candidates (pending_blockers), and
-- linked receipts.

CREATE TABLE IF NOT EXISTS public.thread_brief_cache (
    -- One cache row per thread; cascade delete with the thread
    thread_id       UUID    PRIMARY KEY
                        REFERENCES public.threads(thread_id) ON DELETE CASCADE,

    -- Tenant scope (denormalized from threads for RLS)
    tenant_id       UUID    NOT NULL,
    suite_id        UUID    NOT NULL,

    -- Rolled-up summary text
    summary             TEXT    NULL,

    -- Last unfulfilled commitment captured in this thread
    last_promise        TEXT    NULL,

    -- Open blockers (proactive candidates surfaced for this thread)
    pending_blockers    JSONB   NOT NULL DEFAULT '[]'::jsonb,

    -- Latest receipt linked to this thread (TEXT PK from receipts)
    latest_receipt_id   TEXT    NULL,

    -- Recommended next step (text + optional structured payload)
    next_best_action    JSONB   NOT NULL DEFAULT '{}'::jsonb,

    -- Refresh tracking
    last_built_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    freshness_seq       BIGINT  NOT NULL DEFAULT 0
);

COMMENT ON TABLE public.thread_brief_cache IS
    'Per-thread brief snapshot. Refreshed on memory write to the thread + 60s sweep. '
    'Cascade-deleted when the thread is removed. RLS: tenant_id + suite_id (Law #6).';

COMMENT ON COLUMN public.thread_brief_cache.last_promise IS
    'Latest pending_intent.summary for the thread — the open commitment we owe.';

COMMENT ON COLUMN public.thread_brief_cache.pending_blockers IS
    'JSON array of open proactive_candidates linked to this thread, with id/why_now/risk_tier.';

COMMENT ON COLUMN public.thread_brief_cache.next_best_action IS
    'Structured next-step recommendation: {"text":"...","candidate_id":"...","action_class":"..."}.';

-- Staleness scan (per tenant)
CREATE INDEX IF NOT EXISTS idx_thread_brief_cache_staleness
    ON public.thread_brief_cache (tenant_id, suite_id, last_built_at DESC);

-- RLS
ALTER TABLE public.thread_brief_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.thread_brief_cache FORCE ROW LEVEL SECURITY;

CREATE POLICY thread_brief_cache_select_tenant ON public.thread_brief_cache
    FOR SELECT TO authenticated
    USING (app.is_member(tenant_id::text));

CREATE POLICY thread_brief_cache_all_service_role ON public.thread_brief_cache
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

CREATE POLICY thread_brief_cache_insert_tenant ON public.thread_brief_cache
    FOR INSERT TO authenticated
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
        AND suite_id = public.current_suite_id()
    );

CREATE POLICY thread_brief_cache_update_tenant ON public.thread_brief_cache
    FOR UPDATE TO authenticated
    USING (app.is_member(tenant_id::text))
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

GRANT SELECT, INSERT, UPDATE ON public.thread_brief_cache TO authenticated;
GRANT ALL ON public.thread_brief_cache TO service_role;
