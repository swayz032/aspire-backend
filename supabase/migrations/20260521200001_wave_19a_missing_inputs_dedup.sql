-- Wave 19a: Dedup blueprint_missing_inputs — UNIQUE constraint on (project_id, description_hash)
-- Prevents the same description text from being inserted twice for the same project
-- by either emitter (discipline_verifier or story_writer._call_b).

-- Step 1: Add generated column description_hash = md5(description)
ALTER TABLE public.blueprint_missing_inputs
  ADD COLUMN IF NOT EXISTS description_hash text
    GENERATED ALWAYS AS (md5(description)) STORED;

-- Step 2: One-time cleanup of any exact-text duplicates, keeping the oldest row
-- per (project_id, description_hash). CTE uses row_number to rank by created_at.
WITH ranked AS (
  SELECT
    id,
    ROW_NUMBER() OVER (
      PARTITION BY project_id, md5(description)
      ORDER BY created_at ASC
    ) AS rn
  FROM public.blueprint_missing_inputs
)
DELETE FROM public.blueprint_missing_inputs
WHERE id IN (
  SELECT id FROM ranked WHERE rn > 1
);

-- Step 3: Add UNIQUE constraint on (project_id, description_hash)
ALTER TABLE public.blueprint_missing_inputs
  ADD CONSTRAINT uq_blueprint_missing_inputs_project_desc_hash
  UNIQUE (project_id, description_hash);
