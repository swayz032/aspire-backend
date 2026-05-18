-- Migration: 20260517000005_procure_metadata
-- Wave 5 PROCURE: add tariff_exposure_usd column to blueprint_materials.
--
-- blueprint_materials already has:
--   tariff_flag  tariff_flag   (enum: section_232_steel | section_232_aluminum | softwood_lumber | none)
--   supplier_id  text          (nullable — set by PROCURE stage)
--
-- This migration adds tariff_exposure_usd for estimated dollar impact calculation.
-- Nullable because unit cost may not be available at PROCURE time.
--
-- Law #6: No RLS policy changes — existing policies already scope to suite_id.
-- Law #2: Additive only — no UPDATE/DELETE on existing rows.

ALTER TABLE blueprint_materials
    ADD COLUMN IF NOT EXISTS tariff_exposure_usd numeric(12, 2) DEFAULT NULL;

COMMENT ON COLUMN blueprint_materials.tariff_exposure_usd IS
    'Estimated tariff surcharge in USD for this material line. '
    'Formula: quantity × unit_cost_usd × (tariff_rate / 100). '
    'NULL when unit_cost not yet available from supplier pricing. '
    'Set by Drew PROCURE stage (Wave 5).';
