-- Migration 060: Enable pgvector extension for Clara RAG Knowledge Base
-- Required for vector similarity search (text-embedding-3-large → 3072 dimensions)

CREATE EXTENSION IF NOT EXISTS vector;

COMMENT ON EXTENSION vector IS 'pgvector: vector similarity search for Clara RAG legal knowledge base';
