-- Migration 062: RAG Security Hardening
-- Fix R-001: SECURITY DEFINER RLS bypass in search_legal_knowledge
-- Adds suite_id validation: when app.current_suite_id is set, p_suite_id MUST match (Law #6)

CREATE OR REPLACE FUNCTION public.search_legal_knowledge(
    query_embedding vector(3072),
    query_text TEXT DEFAULT '',
    p_domain TEXT DEFAULT NULL,
    p_template_key TEXT DEFAULT NULL,
    p_template_lane TEXT DEFAULT NULL,
    p_jurisdiction_state TEXT DEFAULT NULL,
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
    template_key TEXT,
    template_lane TEXT,
    jurisdiction_state TEXT,
    confidence_score FLOAT,
    attorney_reviewed BOOLEAN,
    vector_similarity FLOAT,
    text_rank FLOAT,
    combined_score FLOAT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_current_suite UUID;
BEGIN
    -- Law #6 Defense-in-depth: when app.current_suite_id is set (user-facing calls),
    -- validate that p_suite_id matches. Service-role calls without current_suite_id
    -- are trusted (ingestion pipeline, admin operations).
    BEGIN
        v_current_suite := current_setting('app.current_suite_id', true)::uuid;
    EXCEPTION WHEN OTHERS THEN
        v_current_suite := NULL;
    END;

    IF v_current_suite IS NOT NULL
       AND p_suite_id IS NOT NULL
       AND p_suite_id != v_current_suite THEN
        RAISE EXCEPTION 'Access denied: suite_id mismatch (Law #6 tenant isolation)';
    END IF;

    -- Use validated suite_id: prefer current_setting when available
    IF v_current_suite IS NOT NULL AND p_suite_id IS NULL THEN
        p_suite_id := v_current_suite;
    END IF;

    RETURN QUERY
    WITH vector_search AS (
        SELECT
            c.id,
            c.content,
            c.domain,
            c.subdomain,
            c.chunk_type,
            c.template_key,
            c.template_lane,
            c.jurisdiction_state,
            c.confidence_score,
            c.attorney_reviewed,
            1 - (c.embedding <=> query_embedding) AS vsim
        FROM public.legal_knowledge_chunks c
        WHERE c.is_active = true
          -- Tenant isolation: global (NULL) + tenant-specific
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          -- Optional filters
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_template_key IS NULL OR c.template_key = p_template_key)
          AND (p_template_lane IS NULL OR c.template_lane = p_template_lane)
          AND (p_jurisdiction_state IS NULL OR c.jurisdiction_state = p_jurisdiction_state)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        ORDER BY c.embedding <=> query_embedding
        LIMIT p_limit * 3  -- Over-fetch for merge
    ),
    text_search AS (
        SELECT
            c.id,
            ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', query_text)) AS trank
        FROM public.legal_knowledge_chunks c
        WHERE c.is_active = true
          AND query_text IS NOT NULL
          AND query_text != ''
          AND c.content_tsv @@ websearch_to_tsquery('english', query_text)
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_template_key IS NULL OR c.template_key = p_template_key)
          AND (p_template_lane IS NULL OR c.template_lane = p_template_lane)
          AND (p_jurisdiction_state IS NULL OR c.jurisdiction_state = p_jurisdiction_state)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        LIMIT p_limit * 3
    )
    SELECT
        vs.id,
        vs.content,
        vs.domain,
        vs.subdomain,
        vs.chunk_type,
        vs.template_key,
        vs.template_lane,
        vs.jurisdiction_state,
        vs.confidence_score,
        vs.attorney_reviewed,
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

COMMENT ON FUNCTION public.search_legal_knowledge IS
    'Hybrid vector + full-text search for Clara RAG. Defense-in-depth: validates suite_id against app.current_suite_id when set (Law #6).';
