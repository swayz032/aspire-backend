-- =============================================================================
-- Migration 095: Memory Spine — Thread Registry
-- =============================================================================
-- Creates the canonical thread registry for the Office Memory Engine and
-- Coordination Spine V1. Every artifact, memory object, workflow action,
-- receipt, and approval must attach to one canonical thread.
--
-- Aspire Laws enforced:
--   Law #2 (Receipt for All)  — threads record latest_receipt_id
--   Law #3 (Fail Closed)      — RLS denies by default; explicit grants only
--   Law #6 (Tenant Isolation) — tenant_id + suite_id + office_id on every row,
--                               FORCE ROW LEVEL SECURITY
--
-- Finance thread subtypes capture the 11 finance continuity categories.
-- Thread types: 13 canonical domain types + 'client_thread'.
-- Status lifecycle: open → closed → archived (immutable once archived).
--
-- References:
--   03_THREAD_REGISTRY_AND_MEMORY_MODEL.md §3.1
--   02_SHARED_SCHEMAS.md §2.1 (ScopedIdentity)
-- =============================================================================

-- =============================================================================
-- TABLE: public.threads
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.threads (
    -- Primary key
    thread_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Tenant / suite / office scope (Law #6) — all three required
    tenant_id       UUID        NOT NULL,
    suite_id        UUID        NOT NULL,
    office_id       UUID        NOT NULL,

    -- Thread classification
    thread_type     TEXT        NOT NULL CHECK (thread_type IN (
                                    'lead_thread',
                                    'customer_thread',
                                    'deal_thread',
                                    'job_thread',
                                    'project_thread',
                                    'estimate_thread',
                                    'quote_thread',
                                    'invoice_thread',
                                    'contract_thread',
                                    'meeting_thread',
                                    'finance_thread',
                                    'task_thread',
                                    'internal_thread',
                                    'client_thread'
                                )),

    -- Finance thread sub-classification (only populated when thread_type = 'finance_thread')
    finance_thread_subtype  TEXT    NULL CHECK (
                                finance_thread_subtype IS NULL OR
                                finance_thread_subtype IN (
                                    'collections_case',
                                    'provider_connection_issue',
                                    'reconciliation_cluster',
                                    'categorization_cluster',
                                    'payroll_review',
                                    'tax_review',
                                    'cash_risk_review',
                                    'invoice_aging_review',
                                    'finance_task',
                                    'finance_state_change',
                                    'payment_event'
                                )
                            ),

    -- Canonical entity linkage (optional — attach to a domain object)
    canonical_entity_type   TEXT    NULL,
    canonical_entity_id     UUID    NULL,

    -- Human-readable title
    title           TEXT        NULL,

    -- Lifecycle status
    status          TEXT        NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'closed', 'archived')),

    -- Time model (mandatory per §3.3)
    first_event_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    last_activity_at    TIMESTAMPTZ     NOT NULL DEFAULT now(),

    -- Latest pointers (denormalized for O(1) agent startup reads)
    latest_memory_id    UUID    NULL,
    latest_receipt_id   TEXT    NULL,   -- receipts.receipt_id is TEXT
    latest_approval_id  TEXT    NULL,   -- approval_requests.approval_id is TEXT

    -- Collaboration
    participants    UUID[]  NOT NULL DEFAULT '{}',
    tags            TEXT[]  NOT NULL DEFAULT '{}',

    -- Audit
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

    -- Constraint: finance_thread_subtype only makes sense for finance threads
    CONSTRAINT chk_finance_subtype_scope
        CHECK (
            finance_thread_subtype IS NULL
            OR thread_type = 'finance_thread'
        )
);

COMMENT ON TABLE public.threads IS
    'Canonical thread registry for the Office Memory Engine. '
    'Every memory object, receipt, approval, and workflow run attaches to one thread. '
    'RLS: tenant_id + suite_id + office_id triple-scoped (Law #6). '
    'Do not UPDATE status to archived — treat archived threads as immutable.';

COMMENT ON COLUMN public.threads.finance_thread_subtype IS
    'Populated only when thread_type = ''finance_thread''. '
    'Encodes the 11 finance continuity categories.';

COMMENT ON COLUMN public.threads.latest_receipt_id IS
    'Denormalized pointer to receipts.receipt_id (TEXT PK). Updated by trigger on memory_objects insert.';

COMMENT ON COLUMN public.threads.latest_approval_id IS
    'Denormalized pointer to approval_requests.approval_id (TEXT PK).';

COMMENT ON COLUMN public.threads.participants IS
    'Array of actor/user UUIDs who have interacted with this thread.';

-- =============================================================================
-- INDEXES
-- =============================================================================

-- Primary workhorse: tenant-scoped list queries ordered by recency
CREATE INDEX IF NOT EXISTS idx_threads_tenant_scope_activity
    ON public.threads (tenant_id, suite_id, office_id, status, last_activity_at DESC);

-- Entity linkage: "find all threads for this customer/deal/job"
CREATE INDEX IF NOT EXISTS idx_threads_entity_linkage
    ON public.threads (canonical_entity_type, canonical_entity_id)
    WHERE canonical_entity_id IS NOT NULL;

-- Tag search (GIN for array containment @> queries)
CREATE INDEX IF NOT EXISTS idx_threads_tags_gin
    ON public.threads USING GIN (tags);

-- Participant search (GIN for array containment @> queries)
CREATE INDEX IF NOT EXISTS idx_threads_participants_gin
    ON public.threads USING GIN (participants);

-- Finance thread quick-list
CREATE INDEX IF NOT EXISTS idx_threads_finance_subtype
    ON public.threads (tenant_id, suite_id, finance_thread_subtype, last_activity_at DESC)
    WHERE finance_thread_subtype IS NOT NULL;

-- =============================================================================
-- ROW LEVEL SECURITY (Law #3: Fail Closed, Law #6: Tenant Isolation)
-- =============================================================================

ALTER TABLE public.threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.threads FORCE ROW LEVEL SECURITY;

-- SELECT: authenticated users see only their own tenant + suite
-- Uses app.is_member() for auth-context calls (mirrors trust_spine_bundle pattern)
CREATE POLICY threads_select_tenant ON public.threads
    FOR SELECT
    TO authenticated
    USING (app.is_member(tenant_id::text));

-- Service role bypass for orchestrator/backend writes (no auth context in worker calls)
CREATE POLICY threads_all_service_role ON public.threads
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- INSERT: authenticated users may only insert into their own tenant + suite
CREATE POLICY threads_insert_tenant ON public.threads
    FOR INSERT
    TO authenticated
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
        AND suite_id = public.current_suite_id()
    );

-- UPDATE: allowed only within tenant + suite; archived threads become immutable
CREATE POLICY threads_update_tenant ON public.threads
    FOR UPDATE
    TO authenticated
    USING (
        app.is_member(tenant_id::text)
        AND status != 'archived'
    )
    WITH CHECK (
        tenant_id::text IN (
            SELECT tm.tenant_id FROM public.tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
        AND status != 'archived'
    );

-- DELETE: forbidden — threads are append-only; use status='archived'
-- (No DELETE policy = default deny under FORCE ROW LEVEL SECURITY)

-- =============================================================================
-- GRANTS
-- =============================================================================

GRANT SELECT, INSERT, UPDATE ON public.threads TO authenticated;
GRANT ALL ON public.threads TO service_role;
