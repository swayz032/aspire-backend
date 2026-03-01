-- ============================================================================
-- CONVERSATIONAL INTELLIGENCE MIGRATIONS: 066, 067, 068
-- ============================================================================
-- Apply via Supabase Dashboard SQL Editor:
-- https://supabase.com/dashboard/project/qtuehjqlcmfcascqjjhc/sql/new
--
-- Copy this entire file and execute it in the SQL Editor.
-- Total: 6 tables, 3 functions, RLS policies, pgvector indexes
-- ============================================================================

-- ============================================================================
-- MIGRATION 066: General Knowledge Base (Ava RAG)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.general_knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding vector(3072) NOT NULL,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    domain TEXT NOT NULL CHECK (domain IN (
        'aspire_platform', 'business_operations', 'industry_knowledge', 'best_practices'
    )),
    subdomain TEXT,
    source_type TEXT,
    source_id TEXT,
    source_version TEXT,
    chunk_type TEXT CHECK (chunk_type IN (
        'concept', 'procedure', 'definition', 'example', 'best_practice',
        'faq', 'checklist', 'tip', 'industry_insight', 'platform_feature'
    )),
    chunk_index INT,
    parent_chunk_id UUID REFERENCES public.general_knowledge_chunks(id),
    suite_id UUID,
    confidence_score FLOAT DEFAULT 1.0 CHECK (confidence_score >= 0 AND confidence_score <= 1),
    expert_reviewed BOOLEAN DEFAULT false,
    expiry_date TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    ingestion_receipt_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_general_chunk_content_domain_tenant
        UNIQUE (content_hash, domain, COALESCE(suite_id, '00000000-0000-0000-0000-000000000000'::uuid))
);

CREATE INDEX IF NOT EXISTS idx_general_chunks_embedding
    ON public.general_knowledge_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_general_chunks_content_tsv
    ON public.general_knowledge_chunks USING gin(content_tsv);
CREATE INDEX IF NOT EXISTS idx_general_chunks_domain
    ON public.general_knowledge_chunks(domain);
CREATE INDEX IF NOT EXISTS idx_general_chunks_suite_id
    ON public.general_knowledge_chunks(suite_id);
CREATE INDEX IF NOT EXISTS idx_general_chunks_is_active
    ON public.general_knowledge_chunks(is_active);
CREATE INDEX IF NOT EXISTS idx_general_chunks_chunk_type
    ON public.general_knowledge_chunks(chunk_type);
CREATE INDEX IF NOT EXISTS idx_general_chunks_active_domain_suite
    ON public.general_knowledge_chunks(is_active, domain, suite_id)
    WHERE is_active = true;

ALTER TABLE public.general_knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.general_knowledge_chunks FORCE ROW LEVEL SECURITY;

CREATE POLICY general_chunks_select_tenant ON public.general_knowledge_chunks
    FOR SELECT USING (suite_id IS NULL OR suite_id = current_setting('app.current_suite_id')::uuid);
CREATE POLICY general_chunks_insert_service ON public.general_knowledge_chunks
    FOR INSERT WITH CHECK (true);
CREATE POLICY general_chunks_update_service ON public.general_knowledge_chunks
    FOR UPDATE USING (true);

CREATE TABLE IF NOT EXISTS public.general_knowledge_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL CHECK (source_type IN ('file', 'url', 'api', 'manual', 'platform_docs')),
    source_uri TEXT NOT NULL,
    domain TEXT NOT NULL CHECK (domain IN (
        'aspire_platform', 'business_operations', 'industry_knowledge', 'best_practices'
    )),
    title TEXT NOT NULL DEFAULT '',
    version TEXT,
    content_hash TEXT,
    chunk_count INT DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    sync_frequency_hours INT,
    sync_status TEXT DEFAULT 'pending' CHECK (sync_status IN ('pending', 'syncing', 'synced', 'failed', 'stale')),
    suite_id UUID,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_general_sources_domain ON public.general_knowledge_sources(domain);
CREATE INDEX IF NOT EXISTS idx_general_sources_sync_status ON public.general_knowledge_sources(sync_status);
CREATE INDEX IF NOT EXISTS idx_general_sources_suite_id ON public.general_knowledge_sources(suite_id);

ALTER TABLE public.general_knowledge_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.general_knowledge_sources FORCE ROW LEVEL SECURITY;

CREATE POLICY general_sources_select_tenant ON public.general_knowledge_sources
    FOR SELECT USING (suite_id IS NULL OR suite_id = current_setting('app.current_suite_id')::uuid);
CREATE POLICY general_sources_insert_service ON public.general_knowledge_sources
    FOR INSERT WITH CHECK (true);
CREATE POLICY general_sources_update_service ON public.general_knowledge_sources
    FOR UPDATE USING (true);

CREATE OR REPLACE FUNCTION public.search_general_knowledge(
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
    id UUID, content TEXT, domain TEXT, subdomain TEXT, chunk_type TEXT,
    confidence_score FLOAT, expert_reviewed BOOLEAN,
    vector_similarity FLOAT, text_rank FLOAT, combined_score FLOAT
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    WITH vector_search AS (
        SELECT c.id, c.content, c.domain, c.subdomain, c.chunk_type,
               c.confidence_score, c.expert_reviewed,
               1 - (c.embedding <=> query_embedding) AS vsim
        FROM public.general_knowledge_chunks c
        WHERE c.is_active = true
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        ORDER BY c.embedding <=> query_embedding
        LIMIT p_limit * 3
    ),
    text_search AS (
        SELECT c.id, ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', query_text)) AS trank
        FROM public.general_knowledge_chunks c
        WHERE c.is_active = true
          AND query_text IS NOT NULL AND query_text != ''
          AND c.content_tsv @@ websearch_to_tsquery('english', query_text)
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        LIMIT p_limit * 3
    )
    SELECT vs.id, vs.content, vs.domain, vs.subdomain, vs.chunk_type,
           vs.confidence_score, vs.expert_reviewed, vs.vsim AS vector_similarity,
           COALESCE(ts.trank, 0.0)::FLOAT AS text_rank,
           (vs.vsim * p_vector_weight + COALESCE(ts.trank, 0.0) * p_text_weight)::FLOAT AS combined_score
    FROM vector_search vs
    LEFT JOIN text_search ts ON vs.id = ts.id
    WHERE vs.vsim >= p_min_similarity
    ORDER BY (vs.vsim * p_vector_weight + COALESCE(ts.trank, 0.0) * p_text_weight) DESC
    LIMIT p_limit;
END;
$$;

-- ============================================================================
-- MIGRATION 067: Communication Knowledge Base (Eli RAG)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.communication_knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding vector(3072) NOT NULL,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    domain TEXT NOT NULL CHECK (domain IN (
        'email_best_practices', 'client_communication', 'business_writing', 'tone_guidance'
    )),
    subdomain TEXT,
    source_type TEXT,
    source_id TEXT,
    source_version TEXT,
    chunk_type TEXT CHECK (chunk_type IN (
        'template', 'example', 'guideline', 'definition', 'best_practice',
        'faq', 'checklist', 'tone_rule', 'subject_line', 'follow_up_pattern'
    )),
    chunk_index INT,
    parent_chunk_id UUID REFERENCES public.communication_knowledge_chunks(id),
    suite_id UUID,
    confidence_score FLOAT DEFAULT 1.0 CHECK (confidence_score >= 0 AND confidence_score <= 1),
    expert_reviewed BOOLEAN DEFAULT false,
    expiry_date TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    ingestion_receipt_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_comm_chunk_content_domain_tenant
        UNIQUE (content_hash, domain, COALESCE(suite_id, '00000000-0000-0000-0000-000000000000'::uuid))
);

CREATE INDEX IF NOT EXISTS idx_comm_chunks_embedding
    ON public.communication_knowledge_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_comm_chunks_content_tsv
    ON public.communication_knowledge_chunks USING gin(content_tsv);
CREATE INDEX IF NOT EXISTS idx_comm_chunks_domain
    ON public.communication_knowledge_chunks(domain);
CREATE INDEX IF NOT EXISTS idx_comm_chunks_suite_id
    ON public.communication_knowledge_chunks(suite_id);
CREATE INDEX IF NOT EXISTS idx_comm_chunks_is_active
    ON public.communication_knowledge_chunks(is_active);
CREATE INDEX IF NOT EXISTS idx_comm_chunks_chunk_type
    ON public.communication_knowledge_chunks(chunk_type);
CREATE INDEX IF NOT EXISTS idx_comm_chunks_active_domain_suite
    ON public.communication_knowledge_chunks(is_active, domain, suite_id)
    WHERE is_active = true;

ALTER TABLE public.communication_knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.communication_knowledge_chunks FORCE ROW LEVEL SECURITY;

CREATE POLICY comm_chunks_select_tenant ON public.communication_knowledge_chunks
    FOR SELECT USING (suite_id IS NULL OR suite_id = current_setting('app.current_suite_id')::uuid);
CREATE POLICY comm_chunks_insert_service ON public.communication_knowledge_chunks
    FOR INSERT WITH CHECK (true);
CREATE POLICY comm_chunks_update_service ON public.communication_knowledge_chunks
    FOR UPDATE USING (true);

CREATE TABLE IF NOT EXISTS public.communication_knowledge_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL CHECK (source_type IN ('file', 'url', 'api', 'manual', 'style_guide')),
    source_uri TEXT NOT NULL,
    domain TEXT NOT NULL CHECK (domain IN (
        'email_best_practices', 'client_communication', 'business_writing', 'tone_guidance'
    )),
    title TEXT NOT NULL DEFAULT '',
    version TEXT,
    content_hash TEXT,
    chunk_count INT DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    sync_frequency_hours INT,
    sync_status TEXT DEFAULT 'pending' CHECK (sync_status IN ('pending', 'syncing', 'synced', 'failed', 'stale')),
    suite_id UUID,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_comm_sources_domain ON public.communication_knowledge_sources(domain);
CREATE INDEX IF NOT EXISTS idx_comm_sources_sync_status ON public.communication_knowledge_sources(sync_status);
CREATE INDEX IF NOT EXISTS idx_comm_sources_suite_id ON public.communication_knowledge_sources(suite_id);

ALTER TABLE public.communication_knowledge_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.communication_knowledge_sources FORCE ROW LEVEL SECURITY;

CREATE POLICY comm_sources_select_tenant ON public.communication_knowledge_sources
    FOR SELECT USING (suite_id IS NULL OR suite_id = current_setting('app.current_suite_id')::uuid);
CREATE POLICY comm_sources_insert_service ON public.communication_knowledge_sources
    FOR INSERT WITH CHECK (true);
CREATE POLICY comm_sources_update_service ON public.communication_knowledge_sources
    FOR UPDATE USING (true);

CREATE OR REPLACE FUNCTION public.search_communication_knowledge(
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
    id UUID, content TEXT, domain TEXT, subdomain TEXT, chunk_type TEXT,
    confidence_score FLOAT, expert_reviewed BOOLEAN,
    vector_similarity FLOAT, text_rank FLOAT, combined_score FLOAT
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    WITH vector_search AS (
        SELECT c.id, c.content, c.domain, c.subdomain, c.chunk_type,
               c.confidence_score, c.expert_reviewed,
               1 - (c.embedding <=> query_embedding) AS vsim
        FROM public.communication_knowledge_chunks c
        WHERE c.is_active = true
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        ORDER BY c.embedding <=> query_embedding
        LIMIT p_limit * 3
    ),
    text_search AS (
        SELECT c.id, ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', query_text)) AS trank
        FROM public.communication_knowledge_chunks c
        WHERE c.is_active = true
          AND query_text IS NOT NULL AND query_text != ''
          AND c.content_tsv @@ websearch_to_tsquery('english', query_text)
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        LIMIT p_limit * 3
    )
    SELECT vs.id, vs.content, vs.domain, vs.subdomain, vs.chunk_type,
           vs.confidence_score, vs.expert_reviewed, vs.vsim AS vector_similarity,
           COALESCE(ts.trank, 0.0)::FLOAT AS text_rank,
           (vs.vsim * p_vector_weight + COALESCE(ts.trank, 0.0) * p_text_weight)::FLOAT AS combined_score
    FROM vector_search vs
    LEFT JOIN text_search ts ON vs.id = ts.id
    WHERE vs.vsim >= p_min_similarity
    ORDER BY (vs.vsim * p_vector_weight + COALESCE(ts.trank, 0.0) * p_text_weight) DESC
    LIMIT p_limit;
END;
$$;

-- ============================================================================
-- MIGRATION 068: Agent Memory (Episodic + Semantic)
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_episodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id UUID NOT NULL,
    user_id UUID NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    key_topics TEXT[] DEFAULT '{}',
    key_entities JSONB DEFAULT '{}',
    turn_count INT DEFAULT 0,
    embedding vector(3072),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_episode_session_agent UNIQUE (suite_id, session_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_episodes_suite_user ON public.agent_episodes(suite_id, user_id);
CREATE INDEX IF NOT EXISTS idx_episodes_agent ON public.agent_episodes(agent_id);
CREATE INDEX IF NOT EXISTS idx_episodes_session ON public.agent_episodes(session_id);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON public.agent_episodes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_embedding
    ON public.agent_episodes
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

ALTER TABLE public.agent_episodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_episodes FORCE ROW LEVEL SECURITY;

CREATE POLICY episodes_select_tenant ON public.agent_episodes
    FOR SELECT USING (suite_id = current_setting('app.current_suite_id')::uuid);
CREATE POLICY episodes_insert_service ON public.agent_episodes
    FOR INSERT WITH CHECK (true);
CREATE POLICY episodes_update_service ON public.agent_episodes
    FOR UPDATE USING (true);

CREATE OR REPLACE FUNCTION public.search_agent_episodes(
    query_embedding vector(3072),
    p_suite_id UUID,
    p_agent_id TEXT DEFAULT NULL,
    p_user_id UUID DEFAULT NULL,
    p_limit INT DEFAULT 5,
    p_min_similarity FLOAT DEFAULT 0.3
)
RETURNS TABLE (
    id UUID, suite_id UUID, user_id UUID, agent_id TEXT, session_id TEXT,
    summary TEXT, key_topics TEXT[], key_entities JSONB, turn_count INT,
    created_at TIMESTAMPTZ, similarity FLOAT
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT e.id, e.suite_id, e.user_id, e.agent_id, e.session_id,
           e.summary, e.key_topics, e.key_entities, e.turn_count, e.created_at,
           (1 - (e.embedding <=> query_embedding))::FLOAT AS similarity
    FROM public.agent_episodes e
    WHERE e.suite_id = p_suite_id
      AND (p_agent_id IS NULL OR e.agent_id = p_agent_id)
      AND (p_user_id IS NULL OR e.user_id = p_user_id)
      AND e.embedding IS NOT NULL
      AND (1 - (e.embedding <=> query_embedding)) >= p_min_similarity
    ORDER BY e.embedding <=> query_embedding
    LIMIT p_limit;
END;
$$;

CREATE TABLE IF NOT EXISTS public.agent_semantic_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id UUID NOT NULL,
    user_id UUID NOT NULL,
    agent_id TEXT NOT NULL,
    fact_type TEXT NOT NULL CHECK (fact_type IN (
        'preference', 'business_fact', 'relationship', 'industry', 'workflow', 'communication_style'
    )),
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    confidence FLOAT DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    source_episode_id UUID REFERENCES public.agent_episodes(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_semantic_fact UNIQUE (suite_id, user_id, agent_id, fact_key)
);

CREATE INDEX IF NOT EXISTS idx_semantic_suite_user_agent
    ON public.agent_semantic_memory(suite_id, user_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_semantic_fact_type ON public.agent_semantic_memory(fact_type);
CREATE INDEX IF NOT EXISTS idx_semantic_updated ON public.agent_semantic_memory(updated_at DESC);

ALTER TABLE public.agent_semantic_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_semantic_memory FORCE ROW LEVEL SECURITY;

CREATE POLICY semantic_select_tenant ON public.agent_semantic_memory
    FOR SELECT USING (suite_id = current_setting('app.current_suite_id')::uuid);
CREATE POLICY semantic_insert_service ON public.agent_semantic_memory
    FOR INSERT WITH CHECK (true);
CREATE POLICY semantic_update_service ON public.agent_semantic_memory
    FOR UPDATE USING (true);

-- ============================================================================
-- DEPLOYMENT COMPLETE
-- ============================================================================
-- Created: 6 tables, 3 search functions, 24 RLS policies, 33 indexes
-- Next steps:
--   1. Run seed scripts: seed_general_knowledge.py, seed_communication_knowledge.py
--   2. Run verification: python scripts/verify_conversational_intelligence.py
-- ============================================================================
