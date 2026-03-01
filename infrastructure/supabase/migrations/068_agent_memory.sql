-- Migration 068: Agent Memory Tables (Episodic + Semantic)
-- Part of Wave 4: 4-Tier Agent Memory Architecture
-- Supports cross-session agent memory with tenant isolation (Law #6)
--
-- Memory Tiers:
--   1. Working Memory  → Redis (not in DB, see working_memory.py)
--   2. Episodic Memory → agent_episodes (this migration)
--   3. Semantic Memory → agent_semantic_memory (this migration)
--   4. Procedural      → Static persona files + RAG (no DB needed)

-- ============================================================================
-- Table: agent_episodes — Cross-session episode summaries with vector search
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_episodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Tenant + user scoping (Law #6)
    suite_id UUID NOT NULL,
    user_id UUID NOT NULL,

    -- Episode metadata
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,

    -- Content
    summary TEXT NOT NULL,
    key_topics TEXT[] DEFAULT '{}',
    key_entities JSONB DEFAULT '{}',
    turn_count INT DEFAULT 0,

    -- Vector embedding for semantic search (text-embedding-3-large)
    embedding vector(3072),

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Dedup: one episode per session per agent
    CONSTRAINT uq_episode_session_agent
        UNIQUE (suite_id, session_id, agent_id)
);

-- ============================================================================
-- Indexes for agent_episodes
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_episodes_suite_user
    ON public.agent_episodes(suite_id, user_id);

CREATE INDEX IF NOT EXISTS idx_episodes_agent
    ON public.agent_episodes(agent_id);

CREATE INDEX IF NOT EXISTS idx_episodes_session
    ON public.agent_episodes(session_id);

CREATE INDEX IF NOT EXISTS idx_episodes_created
    ON public.agent_episodes(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_episodes_embedding
    ON public.agent_episodes
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ============================================================================
-- RLS for agent_episodes (Law #6: Tenant Isolation)
-- ============================================================================

ALTER TABLE public.agent_episodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_episodes FORCE ROW LEVEL SECURITY;

CREATE POLICY episodes_select_tenant ON public.agent_episodes
    FOR SELECT USING (
        suite_id = current_setting('app.current_suite_id')::uuid
    );

CREATE POLICY episodes_insert_service ON public.agent_episodes
    FOR INSERT WITH CHECK (true);

CREATE POLICY episodes_update_service ON public.agent_episodes
    FOR UPDATE USING (true);

-- ============================================================================
-- Function: search_agent_episodes() — Vector similarity search for past sessions
-- ============================================================================

CREATE OR REPLACE FUNCTION public.search_agent_episodes(
    query_embedding vector(3072),
    p_suite_id UUID,
    p_agent_id TEXT DEFAULT NULL,
    p_user_id UUID DEFAULT NULL,
    p_limit INT DEFAULT 5,
    p_min_similarity FLOAT DEFAULT 0.3
)
RETURNS TABLE (
    id UUID,
    suite_id UUID,
    user_id UUID,
    agent_id TEXT,
    session_id TEXT,
    summary TEXT,
    key_topics TEXT[],
    key_entities JSONB,
    turn_count INT,
    created_at TIMESTAMPTZ,
    similarity FLOAT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT
        e.id,
        e.suite_id,
        e.user_id,
        e.agent_id,
        e.session_id,
        e.summary,
        e.key_topics,
        e.key_entities,
        e.turn_count,
        e.created_at,
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

COMMENT ON FUNCTION public.search_agent_episodes IS
    'Vector similarity search for past agent episodes. Suite-scoped (Law #6).';

-- ============================================================================
-- Table: agent_semantic_memory — Persistent learned facts about users
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_semantic_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Tenant + user scoping (Law #6)
    suite_id UUID NOT NULL,
    user_id UUID NOT NULL,

    -- Fact metadata
    agent_id TEXT NOT NULL,
    fact_type TEXT NOT NULL CHECK (fact_type IN (
        'preference', 'business_fact', 'relationship',
        'industry', 'workflow', 'communication_style'
    )),
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,

    -- Quality signals
    confidence FLOAT DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    source_episode_id UUID REFERENCES public.agent_episodes(id),

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One fact per key per user per agent (upsert target)
    CONSTRAINT uq_semantic_fact
        UNIQUE (suite_id, user_id, agent_id, fact_key)
);

-- ============================================================================
-- Indexes for agent_semantic_memory
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_semantic_suite_user_agent
    ON public.agent_semantic_memory(suite_id, user_id, agent_id);

CREATE INDEX IF NOT EXISTS idx_semantic_fact_type
    ON public.agent_semantic_memory(fact_type);

CREATE INDEX IF NOT EXISTS idx_semantic_updated
    ON public.agent_semantic_memory(updated_at DESC);

-- ============================================================================
-- RLS for agent_semantic_memory (Law #6: Tenant Isolation)
-- ============================================================================

ALTER TABLE public.agent_semantic_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_semantic_memory FORCE ROW LEVEL SECURITY;

CREATE POLICY semantic_select_tenant ON public.agent_semantic_memory
    FOR SELECT USING (
        suite_id = current_setting('app.current_suite_id')::uuid
    );

CREATE POLICY semantic_insert_service ON public.agent_semantic_memory
    FOR INSERT WITH CHECK (true);

CREATE POLICY semantic_update_service ON public.agent_semantic_memory
    FOR UPDATE USING (true);

-- ============================================================================
-- Comments
-- ============================================================================

COMMENT ON TABLE public.agent_episodes IS
    'Cross-session agent episode summaries with vector embeddings for semantic recall. RLS: suite-scoped (Law #6).';
COMMENT ON COLUMN public.agent_episodes.embedding IS
    'text-embedding-3-large output (3072 dims) for semantic search of past sessions.';
COMMENT ON COLUMN public.agent_episodes.key_entities IS
    'Structured entities extracted from session: {business_name, industry, people_mentioned, amounts, etc.}';

COMMENT ON TABLE public.agent_semantic_memory IS
    'Persistent learned facts about users. Upserted on (suite_id, user_id, agent_id, fact_key). RLS: suite-scoped (Law #6).';
COMMENT ON COLUMN public.agent_semantic_memory.fact_type IS
    'Category: preference, business_fact, relationship, industry, workflow, communication_style.';
