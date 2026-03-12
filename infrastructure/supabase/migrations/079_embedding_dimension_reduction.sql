-- Migration 079: Embedding Dimension Reduction (Phase 5B)
-- Reduces vector columns from 3072 to 1536 dimensions.
-- text-embedding-3-large at 1536 dims retains ~99% quality vs 3072.
--
-- NOTE: Existing embeddings become incompatible after this migration.
-- A batch re-embedding job must run after this migration completes.
-- Until re-embedding, vector similarity queries will return incorrect results.
--
-- This migration only alters the schema. Re-embedding is a separate operation.

-- ============================================================================
-- Step 1: Drop existing HNSW indexes (they reference the old dimension)
-- ============================================================================

DROP INDEX IF EXISTS idx_legal_chunks_embedding;
DROP INDEX IF EXISTS idx_finance_chunks_embedding;
DROP INDEX IF EXISTS idx_general_chunks_embedding;
DROP INDEX IF EXISTS idx_comm_chunks_embedding;
DROP INDEX IF EXISTS idx_episodes_embedding;
DROP INDEX IF EXISTS idx_conf_chunks_embedding;

-- ============================================================================
-- Step 2: Alter embedding columns from vector(3072) to vector(1536)
-- ============================================================================

-- Legal knowledge chunks (migration 061)
ALTER TABLE public.legal_knowledge_chunks
    ALTER COLUMN embedding TYPE vector(1536);

-- Finance knowledge chunks (migration 065)
ALTER TABLE public.finance_knowledge_chunks
    ALTER COLUMN embedding TYPE vector(1536);

-- General knowledge chunks (migration 066)
ALTER TABLE public.general_knowledge_chunks
    ALTER COLUMN embedding TYPE vector(1536);

-- Communication knowledge chunks (migration 067)
ALTER TABLE public.communication_knowledge_chunks
    ALTER COLUMN embedding TYPE vector(1536);

-- Agent episodes (migration 068)
ALTER TABLE public.agent_episodes
    ALTER COLUMN embedding TYPE vector(1536);

-- Conference knowledge chunks (migration 069)
ALTER TABLE public.conference_knowledge_chunks
    ALTER COLUMN embedding TYPE vector(1536);

-- ============================================================================
-- Step 3: Recreate HNSW indexes with correct dimensions
-- ============================================================================

CREATE INDEX idx_legal_chunks_embedding
    ON public.legal_knowledge_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_finance_chunks_embedding
    ON public.finance_knowledge_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_general_chunks_embedding
    ON public.general_knowledge_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_comm_chunks_embedding
    ON public.communication_knowledge_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_episodes_embedding
    ON public.agent_episodes
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_conf_chunks_embedding
    ON public.conference_knowledge_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ============================================================================
-- Step 4: Update search function signatures (query_embedding parameter)
-- ============================================================================
-- Note: PostgreSQL functions with vector(3072) parameters will still accept
-- vector(1536) inputs since vector is a generic type. However, we recreate
-- the functions with the correct type annotation for documentation accuracy.
-- The full function bodies are preserved from their original migrations.
-- Function recreation is handled by re-applying the search functions with
-- the updated parameter type. This is deferred to avoid duplicating
-- 200+ lines of function bodies here — the functions will accept any
-- vector dimension as input regardless.

-- ============================================================================
-- Step 5: Document the dimension change
-- ============================================================================

COMMENT ON COLUMN public.legal_knowledge_chunks.embedding IS 'text-embedding-3-large at 1536 dims (reduced from 3072 for cost/perf)';
COMMENT ON COLUMN public.finance_knowledge_chunks.embedding IS 'text-embedding-3-large at 1536 dims (reduced from 3072 for cost/perf)';
COMMENT ON COLUMN public.general_knowledge_chunks.embedding IS 'text-embedding-3-large at 1536 dims (reduced from 3072 for cost/perf)';
COMMENT ON COLUMN public.communication_knowledge_chunks.embedding IS 'text-embedding-3-large at 1536 dims (reduced from 3072 for cost/perf)';
COMMENT ON COLUMN public.agent_episodes.embedding IS 'text-embedding-3-large at 1536 dims (reduced from 3072 for cost/perf)';
COMMENT ON COLUMN public.conference_knowledge_chunks.embedding IS 'text-embedding-3-large at 1536 dims (reduced from 3072 for cost/perf)';
