-- Wave 4: Blueprint Story Engine — REASON stage metadata columns
-- Plan: ~/.claude/plans/serene-seeking-hollerith.md §4
--
-- Adds metadata columns to blueprint_story to support the StoryOutput schema
-- returned by write_story() and surfaced in stage_progress metadata.
--
-- Changes are additive (no existing columns modified). RLS inherited from
-- migration 20260517000001 (no policy changes needed).

-- ──────────────────────────────────────────────────────────────────────────────
-- blueprint_story: add model_version + mean_confidence columns
-- ──────────────────────────────────────────────────────────────────────────────
alter table public.blueprint_story
  add column if not exists model_version text;

comment on column public.blueprint_story.model_version
  is 'LLM model used to generate this story phase (e.g. gpt-5.4-mini, gpt-5.2).';

alter table public.blueprint_story
  add column if not exists mean_confidence numeric;

comment on column public.blueprint_story.mean_confidence
  is 'Mean confidence across all tagged facts in this story phase (0.0–1.0).';

-- ──────────────────────────────────────────────────────────────────────────────
-- blueprint_assemblies: no schema changes needed (truth column already exists)
-- blueprint_materials: no schema changes needed (truth + tariff_flag already exist)
-- blueprint_missing_inputs: no schema changes needed
-- ──────────────────────────────────────────────────────────────────────────────

-- ──────────────────────────────────────────────────────────────────────────────
-- DOWN (manual; do not run automatically)
-- ──────────────────────────────────────────────────────────────────────────────
-- alter table public.blueprint_story drop column if exists mean_confidence;
-- alter table public.blueprint_story drop column if exists model_version;
