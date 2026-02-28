-- Migration 065: Finance Knowledge Base tables for Finn RAG
-- pgvector-backed hybrid search (vector + full-text + metadata filtering)
-- Supports 8 domains: tax_strategy, accounting_standards, bookkeeping, payroll_rules,
--   payment_processing, financial_planning, provider_integration, regulatory_compliance
-- RLS: global knowledge (suite_id IS NULL) visible to all, tenant-specific scoped by suite_id

-- ============================================================================
-- Table: finance_knowledge_chunks — Primary retrieval table
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.finance_knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Content
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,  -- SHA-256 for dedup
    embedding vector(3072) NOT NULL,  -- text-embedding-3-large output

    -- Full-text search (auto-generated)
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    -- Classification
    domain TEXT NOT NULL CHECK (domain IN (
        'tax_strategy',
        'accounting_standards',
        'bookkeeping',
        'payroll_rules',
        'payment_processing',
        'financial_planning',
        'provider_integration',
        'regulatory_compliance'
    )),
    subdomain TEXT,
    source_type TEXT,
    source_id TEXT,
    source_version TEXT,

    -- Chunk structure
    chunk_type TEXT CHECK (chunk_type IN (
        'rule', 'definition', 'example', 'strategy', 'checklist',
        'faq', 'provider_spec', 'tax_form', 'calculation',
        'threshold', 'deadline'
    )),
    chunk_index INT,
    parent_chunk_id UUID REFERENCES public.finance_knowledge_chunks(id),

    -- Tenant scoping (Law #6)
    -- NULL = global knowledge (all tenants), non-NULL = tenant-specific
    suite_id UUID,

    -- Finance-specific metadata filters
    provider_name TEXT,           -- plaid, stripe, quickbooks, gusto, adp
    tax_year INT,                 -- e.g. 2025, 2026
    jurisdiction TEXT,            -- federal, state abbreviation (CA, TX, NY)

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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Dedup constraint: same content in same domain for same tenant
    CONSTRAINT uq_finance_chunk_content_domain_tenant
        UNIQUE (content_hash, domain, COALESCE(suite_id, '00000000-0000-0000-0000-000000000000'::uuid))
);

-- ============================================================================
-- Indexes
-- ============================================================================

-- Vector similarity search (IVFFlat — upgradeable to HNSW at >50K chunks)
CREATE INDEX IF NOT EXISTS idx_finance_chunks_embedding
    ON public.finance_knowledge_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Full-text search
CREATE INDEX IF NOT EXISTS idx_finance_chunks_content_tsv
    ON public.finance_knowledge_chunks USING gin(content_tsv);

-- Metadata filter indexes
CREATE INDEX IF NOT EXISTS idx_finance_chunks_domain
    ON public.finance_knowledge_chunks(domain);
CREATE INDEX IF NOT EXISTS idx_finance_chunks_provider_name
    ON public.finance_knowledge_chunks(provider_name);
CREATE INDEX IF NOT EXISTS idx_finance_chunks_tax_year
    ON public.finance_knowledge_chunks(tax_year);
CREATE INDEX IF NOT EXISTS idx_finance_chunks_jurisdiction
    ON public.finance_knowledge_chunks(jurisdiction);
CREATE INDEX IF NOT EXISTS idx_finance_chunks_suite_id
    ON public.finance_knowledge_chunks(suite_id);
CREATE INDEX IF NOT EXISTS idx_finance_chunks_is_active
    ON public.finance_knowledge_chunks(is_active);
CREATE INDEX IF NOT EXISTS idx_finance_chunks_chunk_type
    ON public.finance_knowledge_chunks(chunk_type);

-- Composite index for common query pattern: active + domain + suite
CREATE INDEX IF NOT EXISTS idx_finance_chunks_active_domain_suite
    ON public.finance_knowledge_chunks(is_active, domain, suite_id)
    WHERE is_active = true;

-- ============================================================================
-- RLS (Law #6: Tenant Isolation)
-- ============================================================================

ALTER TABLE public.finance_knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finance_knowledge_chunks FORCE ROW LEVEL SECURITY;

-- SELECT: Global knowledge (suite_id IS NULL) visible to all tenants,
-- tenant-specific knowledge only visible to the owning tenant
CREATE POLICY finance_chunks_select_tenant ON public.finance_knowledge_chunks
    FOR SELECT USING (
        suite_id IS NULL
        OR suite_id = current_setting('app.current_suite_id')::uuid
    );

-- INSERT: Service role only (ingestion pipeline runs with service_role key)
CREATE POLICY finance_chunks_insert_service ON public.finance_knowledge_chunks
    FOR INSERT WITH CHECK (true);

-- UPDATE: Service role only (soft deletes, metadata updates)
CREATE POLICY finance_chunks_update_service ON public.finance_knowledge_chunks
    FOR UPDATE USING (true);

-- No DELETE policy — soft deletes via is_active = false (Law #2 spirit)

-- ============================================================================
-- Table: finance_knowledge_sources — Tracks ingestion sources for audit/refresh
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.finance_knowledge_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Source identification
    source_type TEXT NOT NULL CHECK (source_type IN (
        'file', 'url', 'api', 'manual', 'provider_sync'
    )),
    source_uri TEXT NOT NULL,
    domain TEXT NOT NULL CHECK (domain IN (
        'tax_strategy',
        'accounting_standards',
        'bookkeeping',
        'payroll_rules',
        'payment_processing',
        'financial_planning',
        'provider_integration',
        'regulatory_compliance'
    )),
    title TEXT NOT NULL DEFAULT '',

    -- Versioning
    version TEXT,
    content_hash TEXT,  -- Hash of source content for change detection

    -- Sync tracking
    chunk_count INT DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    sync_frequency_hours INT,
    sync_status TEXT DEFAULT 'pending' CHECK (sync_status IN (
        'pending', 'syncing', 'synced', 'failed', 'stale'
    )),

    -- Tenant scoping (same pattern as chunks)
    suite_id UUID,

    -- Extra metadata
    metadata JSONB DEFAULT '{}',

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_finance_sources_domain
    ON public.finance_knowledge_sources(domain);
CREATE INDEX IF NOT EXISTS idx_finance_sources_sync_status
    ON public.finance_knowledge_sources(sync_status);
CREATE INDEX IF NOT EXISTS idx_finance_sources_suite_id
    ON public.finance_knowledge_sources(suite_id);

-- RLS
ALTER TABLE public.finance_knowledge_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finance_knowledge_sources FORCE ROW LEVEL SECURITY;

CREATE POLICY finance_sources_select_tenant ON public.finance_knowledge_sources
    FOR SELECT USING (
        suite_id IS NULL
        OR suite_id = current_setting('app.current_suite_id')::uuid
    );

CREATE POLICY finance_sources_insert_service ON public.finance_knowledge_sources
    FOR INSERT WITH CHECK (true);

CREATE POLICY finance_sources_update_service ON public.finance_knowledge_sources
    FOR UPDATE USING (true);

-- ============================================================================
-- Function: search_finance_knowledge() — Hybrid vector + full-text search
-- ============================================================================

CREATE OR REPLACE FUNCTION public.search_finance_knowledge(
    query_embedding vector(3072),
    query_text TEXT DEFAULT '',
    p_domain TEXT DEFAULT NULL,
    p_provider_name TEXT DEFAULT NULL,
    p_tax_year INT DEFAULT NULL,
    p_jurisdiction TEXT DEFAULT NULL,
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
    provider_name TEXT,
    tax_year INT,
    jurisdiction TEXT,
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
            c.provider_name,
            c.tax_year,
            c.jurisdiction,
            c.confidence_score,
            c.expert_reviewed,
            1 - (c.embedding <=> query_embedding) AS vsim
        FROM public.finance_knowledge_chunks c
        WHERE c.is_active = true
          -- Tenant isolation: global (NULL) + tenant-specific
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          -- Optional filters
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_provider_name IS NULL OR c.provider_name = p_provider_name)
          AND (p_tax_year IS NULL OR c.tax_year = p_tax_year)
          AND (p_jurisdiction IS NULL OR c.jurisdiction = p_jurisdiction)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        ORDER BY c.embedding <=> query_embedding
        LIMIT p_limit * 3  -- Over-fetch for merge
    ),
    text_search AS (
        SELECT
            c.id,
            ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', query_text)) AS trank
        FROM public.finance_knowledge_chunks c
        WHERE c.is_active = true
          AND query_text IS NOT NULL
          AND query_text != ''
          AND c.content_tsv @@ websearch_to_tsquery('english', query_text)
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_provider_name IS NULL OR c.provider_name = p_provider_name)
          AND (p_tax_year IS NULL OR c.tax_year = p_tax_year)
          AND (p_jurisdiction IS NULL OR c.jurisdiction = p_jurisdiction)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        LIMIT p_limit * 3
    )
    SELECT
        vs.id,
        vs.content,
        vs.domain,
        vs.subdomain,
        vs.chunk_type,
        vs.provider_name,
        vs.tax_year,
        vs.jurisdiction,
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

COMMENT ON FUNCTION public.search_finance_knowledge IS
    'Hybrid vector + full-text search for Finn RAG. Supports domain, provider, tax year, jurisdiction, and tenant filtering.';

-- ============================================================================
-- Comments
-- ============================================================================

COMMENT ON TABLE public.finance_knowledge_chunks IS
    'Finn RAG knowledge base chunks. pgvector embeddings + full-text search. RLS: global + tenant-scoped.';
COMMENT ON COLUMN public.finance_knowledge_chunks.suite_id IS
    'NULL = global knowledge visible to all tenants. Non-NULL = tenant-specific custom knowledge.';
COMMENT ON COLUMN public.finance_knowledge_chunks.content_hash IS
    'SHA-256 hash of content for dedup. Combined with domain + suite_id for unique constraint.';
COMMENT ON COLUMN public.finance_knowledge_chunks.is_active IS
    'Soft delete flag. Deactivated chunks excluded from search but preserved for audit trail.';
COMMENT ON COLUMN public.finance_knowledge_chunks.provider_name IS
    'Financial provider: plaid, stripe, quickbooks, gusto, adp. NULL for general knowledge.';
COMMENT ON COLUMN public.finance_knowledge_chunks.tax_year IS
    'Applicable tax year for tax-related knowledge. NULL for timeless knowledge.';
COMMENT ON COLUMN public.finance_knowledge_chunks.jurisdiction IS
    'Applicable jurisdiction: federal, or 2-letter state code (CA, TX, NY). NULL for universal rules.';

COMMENT ON TABLE public.finance_knowledge_sources IS
    'Tracks ingestion sources for Finn RAG knowledge base. Used for freshness checks and audit.';
