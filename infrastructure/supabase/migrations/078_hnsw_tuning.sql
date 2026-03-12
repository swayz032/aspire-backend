-- Migration 078: HNSW Index Tuning (Phase 4C)
-- Improves vector search quality for 1M+ episode tables by tuning HNSW parameters.
-- Higher m (24 from 16) improves recall at the cost of more memory per node.
-- Higher ef_construction (128 from 64) improves index quality during build.

-- Agent episodes table (from migration 068_agent_memory.sql)
DROP INDEX IF EXISTS idx_episodes_embedding;
CREATE INDEX idx_episodes_embedding
    ON public.agent_episodes USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

-- General RAG knowledge chunks (from migration 066_general_knowledge_base.sql)
DROP INDEX IF EXISTS idx_general_chunks_embedding;
CREATE INDEX idx_general_chunks_embedding
    ON public.general_knowledge_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

-- Communication RAG knowledge chunks (from migration 067_communication_knowledge_base.sql)
DROP INDEX IF EXISTS idx_comm_chunks_embedding;
CREATE INDEX idx_comm_chunks_embedding
    ON public.communication_knowledge_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

-- Set ef_search for queries (session-level, applied by Supabase connection pooler)
-- This is a runtime parameter, not an index parameter.
-- Applications should SET LOCAL hnsw.ef_search = 100; before vector queries.
-- Default is 40; increasing to 100 improves recall for top-k queries at slight latency cost.
COMMENT ON INDEX idx_episodes_embedding IS 'HNSW m=24 ef_construction=128. Set hnsw.ef_search=100 at query time for best recall.';
COMMENT ON INDEX idx_general_chunks_embedding IS 'HNSW m=24 ef_construction=128. Set hnsw.ef_search=100 at query time for best recall.';
COMMENT ON INDEX idx_comm_chunks_embedding IS 'HNSW m=24 ef_construction=128. Set hnsw.ef_search=100 at query time for best recall.';
