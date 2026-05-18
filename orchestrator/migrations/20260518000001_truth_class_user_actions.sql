-- Migration: 20260518000001_truth_class_user_actions
-- Wave 5.1a-5: add user_skipped and user_overridden to the truth_class enum.
--
-- These values are written by Drew's MATERIAL_OVERRIDE + MATERIAL_SKIP tasks
-- (append-only, Law #2). Rows with these truth values supersede original Drew
-- picks via the supersedes_id FK on blueprint_materials.
--
-- Law #6: No RLS policy changes — existing suite_id-scoped policies apply.
-- Law #2: Additive only — no UPDATE/DELETE on existing rows.
-- Rollback: cannot remove enum values in Postgres without table rewrite;
--   deploy is forward-only. Feature-flag if rollback is required.

ALTER TYPE truth_class ADD VALUE IF NOT EXISTS 'user_skipped';
ALTER TYPE truth_class ADD VALUE IF NOT EXISTS 'user_overridden';

COMMENT ON TYPE truth_class IS
    'Source-of-truth classification for blueprint data rows. '
    'user_skipped: user explicitly skipped this Drew-sourced material (Wave 5.1a-5). '
    'user_overridden: user replaced a Drew pick with their own specification (Wave 5.1a-5).';
