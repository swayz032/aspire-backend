-- Wave 20.6 Stage 5: Spatial Repetition Clustering
-- Adds cluster_id + embedding columns to blueprint_symbols.
-- cluster_id: stable SHA-based cluster identifier written by symbol_cluster_engine.py
-- embedding: 64-dim float32 = 256 bytes; written by symbol_family_embedder (Stage 3)

ALTER TABLE public.blueprint_symbols
  ADD COLUMN IF NOT EXISTS cluster_id  text,
  ADD COLUMN IF NOT EXISTS embedding   bytea;  -- 64-dim float32 = 256 bytes per row

CREATE INDEX IF NOT EXISTS idx_blueprint_symbols_cluster
  ON public.blueprint_symbols (cluster_id)
  WHERE cluster_id IS NOT NULL;

COMMENT ON COLUMN public.blueprint_symbols.cluster_id IS
  'Wave 20.6: stable cluster identifier from symbol_cluster_engine.py Stage 5 single-linkage grouping.';
COMMENT ON COLUMN public.blueprint_symbols.embedding IS
  'Wave 20.6: 64-dim float32 geometric embedding from symbol_family_embedder (Stage 3). 256 bytes.';
