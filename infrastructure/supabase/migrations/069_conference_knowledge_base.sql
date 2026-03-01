-- Migration 069: Conference Knowledge Base for Nora RAG
-- pgvector-backed hybrid search (vector + full-text + metadata filtering)
-- Supports domains: meeting_facilitation, risk_routing, action_items, calendar_optimization, post_meeting_workflows, meeting_intelligence
-- RLS: global knowledge (suite_id IS NULL) visible to all, tenant-specific scoped by suite_id

-- ============================================================================
-- Table: conference_knowledge_chunks — Primary retrieval table
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.conference_knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Content
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,  -- SHA-256 for dedup
    embedding vector(3072) NOT NULL,  -- text-embedding-3-large output

    -- Full-text search (auto-generated)
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    -- Classification
    domain TEXT NOT NULL CHECK (domain IN (
        'meeting_facilitation',
        'risk_routing',
        'action_items',
        'calendar_optimization',
        'post_meeting_workflows',
        'meeting_intelligence'
    )),
    subdomain TEXT,
    source_type TEXT,
    source_id TEXT,
    source_version TEXT,

    -- Chunk structure
    chunk_type TEXT CHECK (chunk_type IN (
        'agenda_template', 'facilitation_guide', 'risk_pattern', 'routing_rule',
        'action_extraction', 'follow_up_template', 'calendar_strategy', 'scheduling_rule',
        'workflow_automation', 'transcript_analysis', 'meeting_insight', 'best_practice',
        'example', 'definition', 'checklist', 'tip'
    )),
    chunk_index INT,
    parent_chunk_id UUID REFERENCES public.conference_knowledge_chunks(id),

    -- Tenant scoping (Law #6)
    -- NULL = global knowledge (all tenants), non-NULL = tenant-specific
    suite_id UUID,

    -- Quality signals
    confidence_score FLOAT DEFAULT 1.0 CHECK (confidence_score >= 0 AND confidence_score <= 1),
    expert_reviewed BOOLEAN DEFAULT false,
    expiry_date TIMESTAMPTZ,

    -- Soft delete (Law #2 spirit: no hard deletes on knowledge)
    is_active BOOLEAN DEFAULT true,

    -- Audit linkage
    ingestion_receipt_id TEXT,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- Indexes
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_conf_chunks_embedding
    ON public.conference_knowledge_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_conf_chunks_content_tsv
    ON public.conference_knowledge_chunks USING gin(content_tsv);

CREATE INDEX IF NOT EXISTS idx_conf_chunks_domain
    ON public.conference_knowledge_chunks(domain);
CREATE INDEX IF NOT EXISTS idx_conf_chunks_suite_id
    ON public.conference_knowledge_chunks(suite_id);
CREATE INDEX IF NOT EXISTS idx_conf_chunks_is_active
    ON public.conference_knowledge_chunks(is_active);
CREATE INDEX IF NOT EXISTS idx_conf_chunks_chunk_type
    ON public.conference_knowledge_chunks(chunk_type);

CREATE INDEX IF NOT EXISTS idx_conf_chunks_active_domain_suite
    ON public.conference_knowledge_chunks(is_active, domain, suite_id)
    WHERE is_active = true;

-- Dedup constraint (using unique index with expression since UNIQUE constraint can't use COALESCE)
CREATE UNIQUE INDEX IF NOT EXISTS uq_conf_chunk_content_domain_tenant
    ON public.conference_knowledge_chunks(content_hash, domain, COALESCE(suite_id, '00000000-0000-0000-0000-000000000000'::uuid));

-- ============================================================================
-- RLS (Law #6: Tenant Isolation)
-- ============================================================================

ALTER TABLE public.conference_knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conference_knowledge_chunks FORCE ROW LEVEL SECURITY;

CREATE POLICY conf_chunks_select_tenant ON public.conference_knowledge_chunks
    FOR SELECT USING (
        suite_id IS NULL
        OR suite_id = current_setting('app.current_suite_id')::uuid
    );

CREATE POLICY conf_chunks_insert_service ON public.conference_knowledge_chunks
    FOR INSERT WITH CHECK (true);

CREATE POLICY conf_chunks_update_service ON public.conference_knowledge_chunks
    FOR UPDATE USING (true);

-- ============================================================================
-- Table: conference_knowledge_sources — Tracks ingestion sources
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.conference_knowledge_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    source_type TEXT NOT NULL CHECK (source_type IN (
        'file', 'url', 'api', 'manual', 'meeting_templates'
    )),
    source_uri TEXT NOT NULL,
    domain TEXT NOT NULL CHECK (domain IN (
        'meeting_facilitation',
        'risk_routing',
        'action_items',
        'calendar_optimization',
        'post_meeting_workflows',
        'meeting_intelligence'
    )),
    title TEXT NOT NULL DEFAULT '',

    version TEXT,
    content_hash TEXT,

    chunk_count INT DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    sync_frequency_hours INT,
    sync_status TEXT DEFAULT 'pending' CHECK (sync_status IN (
        'pending', 'syncing', 'synced', 'failed', 'stale'
    )),

    suite_id UUID,
    metadata JSONB DEFAULT '{}',

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conf_sources_domain
    ON public.conference_knowledge_sources(domain);
CREATE INDEX IF NOT EXISTS idx_conf_sources_sync_status
    ON public.conference_knowledge_sources(sync_status);
CREATE INDEX IF NOT EXISTS idx_conf_sources_suite_id
    ON public.conference_knowledge_sources(suite_id);

ALTER TABLE public.conference_knowledge_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conference_knowledge_sources FORCE ROW LEVEL SECURITY;

CREATE POLICY conf_sources_select_tenant ON public.conference_knowledge_sources
    FOR SELECT USING (
        suite_id IS NULL
        OR suite_id = current_setting('app.current_suite_id')::uuid
    );

CREATE POLICY conf_sources_insert_service ON public.conference_knowledge_sources
    FOR INSERT WITH CHECK (true);

CREATE POLICY conf_sources_update_service ON public.conference_knowledge_sources
    FOR UPDATE USING (true);

-- ============================================================================
-- Function: search_conference_knowledge() — Hybrid vector + full-text search
-- ============================================================================

CREATE OR REPLACE FUNCTION public.search_conference_knowledge(
    query_embedding vector(3072),
    query_text TEXT DEFAULT '',
    p_domain TEXT DEFAULT NULL,
    p_suite_id UUID DEFAULT NULL,
    p_chunk_types TEXT[] DEFAULT NULL,
    p_limit INT DEFAULT 10,
    p_vector_weight FLOAT DEFAULT 0.7,
    p_text_weight FLOAT DEFAULT 0.3,
    p_min_similarity FLOAT DEFAULT 0.3
)
RETURNS TABLE (
    id UUID,
    content TEXT,
    domain TEXT,
    subdomain TEXT,
    chunk_type TEXT,
    confidence_score FLOAT,
    expert_reviewed BOOLEAN,
    vector_similarity FLOAT,
    text_rank FLOAT,
    combined_score FLOAT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    WITH vector_search AS (
        SELECT
            c.id,
            c.content,
            c.domain,
            c.subdomain,
            c.chunk_type,
            c.confidence_score,
            c.expert_reviewed,
            1 - (c.embedding <=> query_embedding) AS vsim
        FROM public.conference_knowledge_chunks c
        WHERE c.is_active = true
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        ORDER BY c.embedding <=> query_embedding
        LIMIT p_limit * 3
    ),
    text_search AS (
        SELECT
            c.id,
            ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', query_text)) AS trank
        FROM public.conference_knowledge_chunks c
        WHERE c.is_active = true
          AND query_text IS NOT NULL
          AND query_text != ''
          AND c.content_tsv @@ websearch_to_tsquery('english', query_text)
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        LIMIT p_limit * 3
    )
    SELECT
        vs.id,
        vs.content,
        vs.domain,
        vs.subdomain,
        vs.chunk_type,
        vs.confidence_score,
        vs.expert_reviewed,
        vs.vsim AS vector_similarity,
        COALESCE(ts.trank, 0.0)::FLOAT AS text_rank,
        (vs.vsim * p_vector_weight + COALESCE(ts.trank, 0.0) * p_text_weight)::FLOAT AS combined_score
    FROM vector_search vs
    LEFT JOIN text_search ts ON vs.id = ts.id
    WHERE vs.vsim >= p_min_similarity
    ORDER BY (vs.vsim * p_vector_weight + COALESCE(ts.trank, 0.0) * p_text_weight) DESC
    LIMIT p_limit;
END;
$$;

COMMENT ON FUNCTION public.search_conference_knowledge IS
    'Hybrid vector + full-text search for Nora conference RAG. Supports domain and tenant filtering.';

COMMENT ON TABLE public.conference_knowledge_chunks IS
    'Nora conference RAG knowledge base chunks. pgvector embeddings + full-text search. RLS: global + tenant-scoped.';
COMMENT ON COLUMN public.conference_knowledge_chunks.suite_id IS
    'NULL = global knowledge visible to all tenants. Non-NULL = tenant-specific custom knowledge.';
