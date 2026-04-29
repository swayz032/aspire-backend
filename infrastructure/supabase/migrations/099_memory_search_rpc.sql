-- =============================================================================
-- Migration 099: Memory Search RPC — hybrid keyword + vector + 6-tier ranking
-- =============================================================================
-- Adds public.search_memory_objects(...) — the RPC that powers
-- POST /v1/memory/search and the office/finance memory search pages.
--
-- Aspire Laws enforced:
--   Law #2 (Receipt for All)  — search reads do NOT emit receipts (read-only)
--   Law #3 (Fail Closed)      — RAISE EXCEPTION on tenant isolation violation
--   Law #6 (Tenant Isolation) — app.is_member(tenant_id::text) gate before any read
--                               + explicit suite_id / office_id / visibility_scope filters
--   Law #9 (Security)         — SECURITY DEFINER + SET search_path = public,extensions,
--                               no PII echoed in error messages
--
-- Ranking pipeline (§3.4 of "Office Memory Engine" plan):
--   Tier 1: Exact entity match            score = 1.0
--   Tier 2: Exact thread match            score = 0.9
--   Tier 3: Approval/receipt relevance    +0.10 boost
--   Tier 4: Recency (last_activity_at)    exp(-ln(2) * days_old / 14.0)
--   Tier 5: Freshness (source_updated_at) +0.05 if newer than last_activity_at - 24h
--   Tier 6: Confidence dampening          * (0.5 + 0.5 * confidence)
--
-- Hybrid retrieval:
--   query_embedding present  -> vector ANN top-100 candidates (cosine distance <=>)
--   query_text present       -> keyword top-100 (websearch_to_tsquery)
--   neither                  -> entity/thread filter only (no full-table scan)
--   final hybrid_score = 0.6 * vector_sim + 0.4 * keyword_rank, then tier boosts applied.
--   Recency weight is dampened to 0.25 so it acts as a tie-breaker rather than dominating
--   exact entity / thread / hybrid matches.
--
-- HNSW tuning: PERFORM set_config('hnsw.ef_search','100',true) inside function
--
-- search_path includes 'extensions' because pgvector lives in extensions schema
-- on Supabase; without it, the <=> operator parses to extensions.vector <=> extensions.vector
-- and the planner cannot resolve it even when the vector CTE is short-circuited at runtime.
--
-- References:
--   plan §3.4 (Hybrid retrieval + ranking)
--   migration 096 (memory_objects HNSW + tsv layout)
-- =============================================================================

-- Drop any pre-existing version (defensive: allows re-application)
DROP FUNCTION IF EXISTS public.search_memory_objects(
    UUID, UUID, UUID, TEXT, TEXT, vector(1536),
    TEXT, UUID, UUID, TEXT[], TEXT[],
    TIMESTAMPTZ, TIMESTAMPTZ, FLOAT, INT
);

-- =============================================================================
-- FUNCTION: public.search_memory_objects
-- =============================================================================
-- Returns ranked memory rows + a synthetic 'score' column.
-- Caller is responsible for stripping the embedding column when not requested
-- (the RPC does not return embedding vectors to keep payloads small).

CREATE FUNCTION public.search_memory_objects(
    p_tenant_id          UUID,
    p_suite_id           UUID,
    p_office_id          UUID,
    p_visibility_scope   TEXT          DEFAULT 'office',
    p_query_text         TEXT          DEFAULT NULL,
    p_query_embedding    vector(1536)  DEFAULT NULL,
    p_entity_type        TEXT          DEFAULT NULL,
    p_entity_id          UUID          DEFAULT NULL,
    p_thread_id          UUID          DEFAULT NULL,
    p_memory_types       TEXT[]        DEFAULT NULL,
    p_tags               TEXT[]        DEFAULT NULL,
    p_date_range_start   TIMESTAMPTZ   DEFAULT NULL,
    p_date_range_end     TIMESTAMPTZ   DEFAULT NULL,
    p_min_confidence     FLOAT         DEFAULT NULL,
    p_limit              INT           DEFAULT 50
)
RETURNS TABLE (
    memory_id               UUID,
    tenant_id               UUID,
    suite_id                UUID,
    office_id               UUID,
    memory_type             TEXT,
    schema_version          TEXT,
    source_surface          TEXT,
    source_agent            TEXT,
    runtime_family          TEXT,
    channel                 TEXT,
    session_provider        TEXT,
    transcript_provider     TEXT,
    recording_provider      TEXT,
    external_session_id     TEXT,
    source_record_id        TEXT,
    trace_id                UUID,
    correlation_id          UUID,
    artifact_origin         TEXT,
    summary_origin          TEXT,
    entity_type             TEXT,
    entity_id               UUID,
    thread_id               UUID,
    title                   TEXT,
    summary                 TEXT,
    detail                  JSONB,
    confidence              FLOAT,
    visibility_scope        TEXT,
    status                  TEXT,
    linked_receipt_ids      UUID[],
    linked_approval_ids     UUID[],
    linked_artifact_ids     UUID[],
    linked_workflow_run_ids UUID[],
    event_at                TIMESTAMPTZ,
    created_at              TIMESTAMPTZ,
    source_updated_at       TIMESTAMPTZ,
    promoted_at             TIMESTAMPTZ,
    approved_at             TIMESTAMPTZ,
    executed_at             TIMESTAMPTZ,
    last_activity_at        TIMESTAMPTZ,
    summary_window_start_at TIMESTAMPTZ,
    summary_window_end_at   TIMESTAMPTZ,
    fresh_until             TIMESTAMPTZ,
    idempotency_key         TEXT,
    score                   FLOAT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $func$
DECLARE
    v_has_vector  BOOLEAN := p_query_embedding IS NOT NULL;
    v_has_text    BOOLEAN := p_query_text IS NOT NULL AND length(trim(p_query_text)) > 0;
    v_has_entity  BOOLEAN := p_entity_type IS NOT NULL AND p_entity_id IS NOT NULL;
    v_has_thread  BOOLEAN := p_thread_id IS NOT NULL;
    v_now         TIMESTAMPTZ := now();
    v_role        TEXT := coalesce(current_setting('role', true), '');
    v_user        TEXT := current_user;
    v_is_service  BOOLEAN := v_role = 'service_role'
                             OR v_user IN ('service_role', 'supabase_admin', 'postgres');
BEGIN
    -- =========================================================================
    -- Law #6: Tenant isolation gate — fail closed before reading anything
    -- =========================================================================
    -- service_role + DB superuser bypass app.is_member by design; everyone else
    -- must be a member of the tenant.
    IF NOT (v_is_service OR app.is_member(p_tenant_id::text)) THEN
        RAISE EXCEPTION 'TENANT_ISOLATION_VIOLATION: caller is not a member of tenant %', p_tenant_id
            USING ERRCODE = '42501';
    END IF;

    -- =========================================================================
    -- Empty-search guard: never full-scan when caller gave us nothing to anchor.
    -- =========================================================================
    IF NOT v_has_vector AND NOT v_has_text AND NOT v_has_entity AND NOT v_has_thread THEN
        RETURN;
    END IF;

    -- =========================================================================
    -- HNSW tuning (transaction-scoped so it does not leak to the caller's session)
    -- =========================================================================
    PERFORM set_config('hnsw.ef_search', '100', true);

    -- =========================================================================
    -- Build candidate set + per-row score in a single CTE pipeline.
    -- =========================================================================
    RETURN QUERY
    WITH
    base AS (
        -- Mandatory tenant + visibility filter (Law #6 defense-in-depth)
        SELECT m.*
        FROM public.memory_objects m
        WHERE m.tenant_id        = p_tenant_id
          AND m.suite_id         = p_suite_id
          AND m.office_id        = p_office_id
          AND m.visibility_scope = p_visibility_scope
          AND (m.status IS NULL OR m.status NOT IN ('rejected', 'superseded'))
          AND (p_memory_types IS NULL OR m.memory_type = ANY(p_memory_types))
          AND (p_min_confidence IS NULL OR (m.confidence IS NOT NULL AND m.confidence >= p_min_confidence))
          AND (p_date_range_start IS NULL OR coalesce(m.event_at, m.created_at) >= p_date_range_start)
          AND (p_date_range_end   IS NULL OR coalesce(m.event_at, m.created_at) <= p_date_range_end)
          AND (
              p_tags IS NULL
              OR (
                  m.detail ? 'tags'
                  AND jsonb_typeof(m.detail->'tags') = 'array'
                  AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements_text(m.detail->'tags') AS t(tag)
                      WHERE t.tag = ANY(p_tags)
                  )
              )
          )
    ),
    -- ANN candidates: top-100 by cosine distance against query embedding
    vec_candidates AS (
        SELECT b.*,
               (1.0 - (b.embedding <=> p_query_embedding))::float AS vec_sim
        FROM base b
        WHERE v_has_vector
          AND b.embedding IS NOT NULL
        ORDER BY b.embedding <=> p_query_embedding
        LIMIT 100
    ),
    -- Keyword candidates: top-100 by ts_rank_cd against websearch query
    kw_candidates AS (
        SELECT b.*,
               ts_rank_cd(b.tsv, websearch_to_tsquery('english', p_query_text))::float AS kw_rank
        FROM base b
        WHERE v_has_text
          AND b.tsv @@ websearch_to_tsquery('english', p_query_text)
        ORDER BY ts_rank_cd(b.tsv, websearch_to_tsquery('english', p_query_text)) DESC
        LIMIT 100
    ),
    -- Entity / thread anchor candidates (always included when caller requested them)
    anchor_candidates AS (
        SELECT b.*
        FROM base b
        WHERE
            (v_has_entity AND b.entity_type = p_entity_type AND b.entity_id = p_entity_id)
            OR (v_has_thread AND b.thread_id = p_thread_id)
    ),
    -- Union-by-memory_id of every candidate source. We keep one row per memory.
    candidates AS (
        SELECT vc.memory_id FROM vec_candidates vc
        UNION
        SELECT kc.memory_id FROM kw_candidates kc
        UNION
        SELECT ac.memory_id FROM anchor_candidates ac
    ),
    -- Bring every base column back + per-source scores (NULL when source didn't match)
    enriched AS (
        SELECT b.*,
               vc.vec_sim,
               kc.kw_rank
        FROM base b
        JOIN candidates c ON c.memory_id = b.memory_id
        LEFT JOIN vec_candidates vc ON vc.memory_id = b.memory_id
        LEFT JOIN kw_candidates  kc ON kc.memory_id = b.memory_id
    ),
    -- Normalize keyword rank to [0, 1] using the max within the result set.
    -- ts_rank_cd is unbounded; normalization keeps the hybrid weighting stable.
    kw_norm AS (
        SELECT max(kw_rank) AS max_kw FROM enriched WHERE kw_rank IS NOT NULL
    ),
    scored AS (
        SELECT
            e.*,
            -- ---------- vector + keyword hybrid (only counts what was provided) ----------
            CASE
                WHEN v_has_vector AND v_has_text THEN
                    0.6 * coalesce(e.vec_sim, 0.0)
                    + 0.4 * coalesce(
                        e.kw_rank / NULLIF((SELECT max_kw FROM kw_norm), 0),
                        0.0
                    )
                WHEN v_has_vector THEN
                    coalesce(e.vec_sim, 0.0)
                WHEN v_has_text THEN
                    coalesce(
                        e.kw_rank / NULLIF((SELECT max_kw FROM kw_norm), 0),
                        0.0
                    )
                ELSE 0.0
            END AS hybrid_score,

            -- ---------- Tier 1: exact entity match ----------
            CASE
                WHEN v_has_entity
                     AND e.entity_type = p_entity_type
                     AND e.entity_id = p_entity_id
                THEN 1.0
                ELSE 0.0
            END AS tier1_entity,

            -- ---------- Tier 2: exact thread match ----------
            CASE
                WHEN v_has_thread AND e.thread_id = p_thread_id THEN 0.9
                ELSE 0.0
            END AS tier2_thread,

            -- ---------- Tier 3: approval / receipt relevance ----------
            CASE
                WHEN coalesce(array_length(e.linked_receipt_ids, 1), 0) > 0
                  OR coalesce(array_length(e.linked_approval_ids, 1), 0) > 0
                THEN 0.10
                ELSE 0.0
            END AS tier3_appreceipt,

            -- ---------- Tier 4: recency (exp half-life 14 days) ----------
            -- exp(-ln(2) * days_old / 14)
            exp(
                -0.6931471805599453
                * GREATEST(0.0, EXTRACT(EPOCH FROM (v_now - e.last_activity_at)) / 86400.0)
                / 14.0
            )::float AS tier4_recency,

            -- ---------- Tier 5: freshness (source_updated_at vs activity) ----------
            CASE
                WHEN e.source_updated_at IS NOT NULL
                     AND e.source_updated_at >= (e.last_activity_at - INTERVAL '24 hours')
                THEN 0.05
                ELSE 0.0
            END AS tier5_freshness,

            -- ---------- Tier 6: confidence dampening multiplier ----------
            CASE
                WHEN e.confidence IS NULL THEN 1.0
                ELSE (0.5 + 0.5 * e.confidence)::float
            END AS tier6_conf_mult
        FROM enriched e
    ),
    final_set AS (
        SELECT
            s.*,
            -- Final score formula (§3.4):
            --   anchor_floor = max(tier1_entity, tier2_thread)        - exact-match floor
            --   merit        = anchor_floor + hybrid_score
            --                + tier3_appreceipt + tier5_freshness
            --                + tier4_recency * 0.25                   - recency damped weight
            --   final        = merit * tier6_conf_mult
            -- Recency is multiplied by 0.25 so it acts as a tiebreaker
            -- (full 1.0 weight would dominate hybrid sims).
            (
                (GREATEST(s.tier1_entity, s.tier2_thread)
                 + s.hybrid_score
                 + s.tier3_appreceipt
                 + s.tier5_freshness
                 + 0.25 * s.tier4_recency)
                * s.tier6_conf_mult
            )::float AS computed_score
        FROM scored s
    )
    SELECT
        f.memory_id,
        f.tenant_id,
        f.suite_id,
        f.office_id,
        f.memory_type,
        f.schema_version,
        f.source_surface,
        f.source_agent,
        f.runtime_family,
        f.channel,
        f.session_provider,
        f.transcript_provider,
        f.recording_provider,
        f.external_session_id,
        f.source_record_id,
        f.trace_id,
        f.correlation_id,
        f.artifact_origin,
        f.summary_origin,
        f.entity_type,
        f.entity_id,
        f.thread_id,
        f.title,
        f.summary,
        f.detail,
        f.confidence,
        f.visibility_scope,
        f.status,
        f.linked_receipt_ids,
        f.linked_approval_ids,
        f.linked_artifact_ids,
        f.linked_workflow_run_ids,
        f.event_at,
        f.created_at,
        f.source_updated_at,
        f.promoted_at,
        f.approved_at,
        f.executed_at,
        f.last_activity_at,
        f.summary_window_start_at,
        f.summary_window_end_at,
        f.fresh_until,
        f.idempotency_key,
        f.computed_score AS score
    FROM final_set f
    ORDER BY f.computed_score DESC, f.last_activity_at DESC
    LIMIT GREATEST(1, p_limit);
END;
$func$;

COMMENT ON FUNCTION public.search_memory_objects IS
    'Hybrid memory search with 6-tier §3.4 ranking. '
    'Tenant isolation enforced via app.is_member() + explicit scope filters. '
    'SECURITY DEFINER, search_path=public,extensions. '
    'Returns ranked rows + synthetic score column (FLOAT).';

-- =============================================================================
-- GRANTS
-- =============================================================================

-- Postgres defaults grant EXECUTE to PUBLIC on functions; revoke that
-- (and the implicit anon role grant) so only authenticated + service_role
-- can call the search RPC.
REVOKE EXECUTE ON FUNCTION public.search_memory_objects(
    UUID, UUID, UUID, TEXT, TEXT, vector(1536),
    TEXT, UUID, UUID, TEXT[], TEXT[],
    TIMESTAMPTZ, TIMESTAMPTZ, FLOAT, INT
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.search_memory_objects(
    UUID, UUID, UUID, TEXT, TEXT, vector(1536),
    TEXT, UUID, UUID, TEXT[], TEXT[],
    TIMESTAMPTZ, TIMESTAMPTZ, FLOAT, INT
) FROM anon;

GRANT EXECUTE ON FUNCTION public.search_memory_objects(
    UUID, UUID, UUID, TEXT, TEXT, vector(1536),
    TEXT, UUID, UUID, TEXT[], TEXT[],
    TIMESTAMPTZ, TIMESTAMPTZ, FLOAT, INT
) TO authenticated, service_role;
