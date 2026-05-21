-- Migration: 20260521000001_blueprint_materials_assembly_id
-- Wave 17b.5: link blueprint_materials rows back to the parent assembly so
-- the Takeoff UI can click a material row and highlight the source assembly +
-- the underlying symbols on the sheet.
--
-- Nullable FK because some materials originate from standalone symbols
-- (e.g. an isolated GFCI receptacle that does not roll up into a typed
-- assembly). ON DELETE SET NULL preserves the material row when an
-- assembly is superseded.
--
-- Law #2: additive only — no UPDATE/DELETE on existing rows.
-- Law #6: no RLS changes — existing suite_id policies already cover this column.

ALTER TABLE blueprint_materials
    ADD COLUMN IF NOT EXISTS assembly_id uuid
    REFERENCES blueprint_assemblies(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS blueprint_materials_assembly_idx
    ON blueprint_materials (assembly_id)
    WHERE assembly_id IS NOT NULL;

COMMENT ON COLUMN blueprint_materials.assembly_id IS
    'Optional FK to the parent blueprint_assemblies row this material derives from. '
    'NULL when the material was procured from a standalone symbol with no rollup. '
    'Set by Drew PROCURE (Wave 17b.5). ON DELETE SET NULL.';
