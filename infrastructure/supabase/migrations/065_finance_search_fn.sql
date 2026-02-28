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
DECLARE
    v_current_suite UUID;
BEGIN
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

    IF v_current_suite IS NOT NULL AND p_suite_id IS NULL THEN
        p_suite_id := v_current_suite;
    END IF;

    RETURN QUERY
    WITH vector_search AS (
        SELECT
            c.id, c.content, c.domain, c.subdomain, c.chunk_type,
            c.provider_name, c.tax_year, c.jurisdiction,
            c.confidence_score, c.expert_reviewed,
            1 - (c.embedding <=> query_embedding) AS vsim
        FROM public.finance_knowledge_chunks c
        WHERE c.is_active = true
          AND (c.suite_id IS NULL OR c.suite_id = p_suite_id)
          AND (p_domain IS NULL OR c.domain = p_domain)
          AND (p_provider_name IS NULL OR c.provider_name = p_provider_name)
          AND (p_tax_year IS NULL OR c.tax_year = p_tax_year)
          AND (p_jurisdiction IS NULL OR c.jurisdiction = p_jurisdiction)
          AND (p_chunk_types IS NULL OR c.chunk_type = ANY(p_chunk_types))
        ORDER BY c.embedding <=> query_embedding
        LIMIT p_limit * 3
    ),
    text_search AS (
        SELECT c.id,
            ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', query_text)) AS trank
        FROM public.finance_knowledge_chunks c
        WHERE c.is_active = true
          AND query_text IS NOT NULL AND query_text != ''
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
        vs.id, vs.content, vs.domain, vs.subdomain, vs.chunk_type,
        vs.provider_name, vs.tax_year, vs.jurisdiction,
        vs.confidence_score, vs.expert_reviewed,
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
    'Hybrid vector + full-text search for Finn RAG. Defense-in-depth: validates suite_id (Law #6).';
