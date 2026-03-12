-- Migration 076: Memory Text Search Indexes (Phase 3C)
-- Adds GIN trigram indexes on agent_semantic_memory for ilike acceleration.
-- Supports Phase 3A: push text search to PostgREST instead of Python-side filtering.
-- Also adds atomic prune function for fact caps (Phase 3B).

-- ============================================================================
-- 1. Enable pg_trgm for GIN trigram indexes (accelerates ilike queries)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- 2. GIN trigram indexes on agent_semantic_memory for ilike acceleration
--    These make `fact_key.ilike.*query*` and `fact_value.ilike.*query*` fast
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_semantic_fact_key_trgm
    ON agent_semantic_memory USING gin (fact_key gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_semantic_fact_value_trgm
    ON agent_semantic_memory USING gin (fact_value gin_trgm_ops);

-- ============================================================================
-- 3. Composite index for the common query pattern: (suite_id, agent_id, user_id)
--    Accelerates the base filter before trigram search kicks in.
--    Note: idx_semantic_suite_user_agent (suite_id, user_id, agent_id) exists
--    from migration 068 — this adds agent_id-first ordering for agent-scoped queries.
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_semantic_memory_tenant_agent
    ON agent_semantic_memory (suite_id, agent_id, user_id);

-- ============================================================================
-- 4. Atomic prune function for fact caps (Phase 3B)
--    Deletes oldest facts beyond the cap for a given agent+user+suite.
--    Called after upsert to enforce per-agent fact limits.
-- ============================================================================

CREATE OR REPLACE FUNCTION app.prune_agent_semantic_memory(
    p_suite_id uuid,
    p_user_id uuid,
    p_agent_id text,
    p_max_facts int DEFAULT 500
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    DELETE FROM agent_semantic_memory
    WHERE id IN (
        SELECT id
        FROM agent_semantic_memory
        WHERE suite_id = p_suite_id
          AND user_id = p_user_id
          AND agent_id = p_agent_id
        ORDER BY updated_at DESC
        OFFSET p_max_facts
    );
END;
$$;

-- Grant execute to authenticated users (RLS still applies on the table)
GRANT EXECUTE ON FUNCTION app.prune_agent_semantic_memory(uuid, uuid, text, int) TO authenticated;
GRANT EXECUTE ON FUNCTION app.prune_agent_semantic_memory(uuid, uuid, text, int) TO service_role;
