-- Wave 20.6.3c — Add family_id column to blueprint_project_legends.
--
-- family_id groups legend symbols into geometric families via embedding
-- cosine similarity (computed by symbol_family_embedder.cluster_legend_into_families).
-- NULL = symbol has not been assigned to a family (singleton or not yet clustered).
-- Format: 'fam_<12-char sha1 prefix>' — deterministic across runs.
--
-- Law #2: No receipt needed for schema migration — the clustering function that
--          writes family_id emits its own receipt.
-- Law #6: RLS already enforces suite_id isolation on this table.

ALTER TABLE public.blueprint_project_legends
    ADD COLUMN IF NOT EXISTS family_id text;

CREATE INDEX IF NOT EXISTS idx_bp_project_legends_family
    ON public.blueprint_project_legends (project_id, family_id)
    WHERE family_id IS NOT NULL;
