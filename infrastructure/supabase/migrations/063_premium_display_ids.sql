-- 063: Premium Display IDs
-- Human-friendly short IDs: STE-001, OFF-001, CTR-2026-001, APR-001, RCT-001
-- Law #2 compliant: only ADDS display_id column, never modifies existing receipt fields

BEGIN;

-- ============================================================
-- 1. Sequence counter table
-- ============================================================
CREATE TABLE IF NOT EXISTS public.display_id_sequences (
  entity_type   TEXT    NOT NULL,
  scope_id      UUID,           -- suite_id for scoped entities, NULL for global
  year_scope    INT,            -- year for year-scoped entities like contracts, NULL otherwise
  current_seq   BIGINT  NOT NULL DEFAULT 0,
  PRIMARY KEY (entity_type, COALESCE(scope_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(year_scope, 0))
);

ALTER TABLE public.display_id_sequences ENABLE ROW LEVEL SECURITY;

-- Only service role can touch sequences
CREATE POLICY "Service role only" ON public.display_id_sequences
  USING (false)
  WITH CHECK (false);

-- ============================================================
-- 2. Atomic sequence increment function
-- ============================================================
CREATE OR REPLACE FUNCTION public.next_display_id(
  p_entity_type TEXT,
  p_scope_id    UUID    DEFAULT NULL,
  p_year_scope  INT     DEFAULT NULL
) RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_key_scope UUID := COALESCE(p_scope_id, '00000000-0000-0000-0000-000000000000'::uuid);
  v_key_year  INT  := COALESCE(p_year_scope, 0);
  v_seq       BIGINT;
BEGIN
  -- Advisory lock prevents concurrent collisions per (entity_type, scope, year)
  PERFORM pg_advisory_xact_lock(
    hashtext(p_entity_type || '::' || v_key_scope::text || '::' || v_key_year::text)
  );

  INSERT INTO public.display_id_sequences (entity_type, scope_id, year_scope, current_seq)
  VALUES (p_entity_type, p_scope_id, p_year_scope, 1)
  ON CONFLICT (entity_type, COALESCE(scope_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(year_scope, 0))
  DO UPDATE SET current_seq = display_id_sequences.current_seq + 1
  RETURNING current_seq INTO v_seq;

  RETURN v_seq;
END;
$$;

-- ============================================================
-- 3. Add display_id columns
-- ============================================================

-- Suites (global scope)
ALTER TABLE app.suites ADD COLUMN IF NOT EXISTS display_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_suites_display_id ON app.suites (display_id) WHERE display_id IS NOT NULL;

-- Offices (per-suite scope)
ALTER TABLE app.offices ADD COLUMN IF NOT EXISTS display_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_offices_display_id ON app.offices (suite_id, display_id) WHERE display_id IS NOT NULL;

-- Suite profiles (synced from suites)
ALTER TABLE public.suite_profiles ADD COLUMN IF NOT EXISTS display_id TEXT;
ALTER TABLE public.suite_profiles ADD COLUMN IF NOT EXISTS office_display_id TEXT;

-- Contracts (per-suite, per-year scope)
ALTER TABLE public.contracts ADD COLUMN IF NOT EXISTS display_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_contracts_display_id ON public.contracts (suite_id, display_id) WHERE display_id IS NOT NULL;

-- Approval requests (per-suite scope)
ALTER TABLE public.approval_requests ADD COLUMN IF NOT EXISTS display_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_display_id ON public.approval_requests (suite_id, display_id) WHERE display_id IS NOT NULL;

-- Receipts (per-suite scope)
ALTER TABLE public.receipts ADD COLUMN IF NOT EXISTS display_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_display_id ON public.receipts (suite_id, display_id) WHERE display_id IS NOT NULL;

-- ============================================================
-- 4. BEFORE INSERT triggers
-- ============================================================

-- Suite trigger (global: STE-001)
CREATE OR REPLACE FUNCTION public.trg_suite_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE v_seq BIGINT;
BEGIN
  IF NEW.display_id IS NULL THEN
    v_seq := public.next_display_id('suite', NULL, NULL);
    NEW.display_id := 'STE-' || LPAD(v_seq::text, 3, '0');
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_suite_display_id ON app.suites;
CREATE TRIGGER trg_suite_display_id
  BEFORE INSERT ON app.suites
  FOR EACH ROW EXECUTE FUNCTION public.trg_suite_display_id();

-- Office trigger (per-suite: OFF-001)
CREATE OR REPLACE FUNCTION public.trg_office_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE v_seq BIGINT;
BEGIN
  IF NEW.display_id IS NULL THEN
    v_seq := public.next_display_id('office', NEW.suite_id, NULL);
    NEW.display_id := 'OFF-' || LPAD(v_seq::text, 3, '0');
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_office_display_id ON app.offices;
CREATE TRIGGER trg_office_display_id
  BEFORE INSERT ON app.offices
  FOR EACH ROW EXECUTE FUNCTION public.trg_office_display_id();

-- Contract trigger (per-suite, per-year: CTR-2026-001)
CREATE OR REPLACE FUNCTION public.trg_contract_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_year INT;
  v_seq  BIGINT;
BEGIN
  IF NEW.display_id IS NULL THEN
    v_year := EXTRACT(YEAR FROM COALESCE(NEW.created_at, NOW()));
    v_seq := public.next_display_id('contract', NEW.suite_id, v_year);
    NEW.display_id := 'CTR-' || v_year::text || '-' || LPAD(v_seq::text, 3, '0');
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_contract_display_id ON public.contracts;
CREATE TRIGGER trg_contract_display_id
  BEFORE INSERT ON public.contracts
  FOR EACH ROW EXECUTE FUNCTION public.trg_contract_display_id();

-- Approval request trigger (per-suite: APR-001)
CREATE OR REPLACE FUNCTION public.trg_approval_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE v_seq BIGINT;
BEGIN
  IF NEW.display_id IS NULL THEN
    v_seq := public.next_display_id('approval', NEW.suite_id, NULL);
    NEW.display_id := 'APR-' || LPAD(v_seq::text, 3, '0');
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_approval_display_id ON public.approval_requests;
CREATE TRIGGER trg_approval_display_id
  BEFORE INSERT ON public.approval_requests
  FOR EACH ROW EXECUTE FUNCTION public.trg_approval_display_id();

-- Receipt trigger (per-suite: RCT-001)
CREATE OR REPLACE FUNCTION public.trg_receipt_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE v_seq BIGINT;
BEGIN
  IF NEW.display_id IS NULL THEN
    v_seq := public.next_display_id('receipt', NEW.suite_id, NULL);
    NEW.display_id := 'RCT-' || LPAD(v_seq::text, 3, '0');
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_receipt_display_id ON public.receipts;
CREATE TRIGGER trg_receipt_display_id
  BEFORE INSERT ON public.receipts
  FOR EACH ROW EXECUTE FUNCTION public.trg_receipt_display_id();

-- ============================================================
-- 5. Backfill existing rows (ordered by created_at ASC)
-- ============================================================

-- Backfill suites
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT suite_id FROM app.suites WHERE display_id IS NULL ORDER BY created_at ASC
  LOOP
    UPDATE app.suites
    SET display_id = 'STE-' || LPAD(public.next_display_id('suite', NULL, NULL)::text, 3, '0')
    WHERE suite_id = r.suite_id;
  END LOOP;
END;
$$;

-- Backfill offices
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT office_id, suite_id FROM app.offices WHERE display_id IS NULL ORDER BY created_at ASC
  LOOP
    UPDATE app.offices
    SET display_id = 'OFF-' || LPAD(public.next_display_id('office', r.suite_id, NULL)::text, 3, '0')
    WHERE office_id = r.office_id;
  END LOOP;
END;
$$;

-- Backfill contracts
DO $$
DECLARE r RECORD; v_year INT;
BEGIN
  FOR r IN SELECT id, suite_id, created_at FROM public.contracts WHERE display_id IS NULL ORDER BY created_at ASC
  LOOP
    v_year := EXTRACT(YEAR FROM r.created_at);
    UPDATE public.contracts
    SET display_id = 'CTR-' || v_year::text || '-' || LPAD(public.next_display_id('contract', r.suite_id, v_year)::text, 3, '0')
    WHERE id = r.id;
  END LOOP;
END;
$$;

-- Backfill approval_requests
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT id, suite_id FROM public.approval_requests WHERE display_id IS NULL ORDER BY created_at ASC
  LOOP
    UPDATE public.approval_requests
    SET display_id = 'APR-' || LPAD(public.next_display_id('approval', r.suite_id, NULL)::text, 3, '0')
    WHERE id = r.id;
  END LOOP;
END;
$$;

-- Backfill receipts (may be large — uses loop)
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT receipt_id, suite_id FROM public.receipts WHERE display_id IS NULL ORDER BY created_at ASC
  LOOP
    UPDATE public.receipts
    SET display_id = 'RCT-' || LPAD(public.next_display_id('receipt', r.suite_id, NULL)::text, 3, '0')
    WHERE receipt_id = r.receipt_id;
  END LOOP;
END;
$$;

-- ============================================================
-- 6. Sync suite display_id to suite_profiles
-- ============================================================
UPDATE public.suite_profiles sp
SET display_id = s.display_id
FROM app.suites s
WHERE sp.suite_id = s.suite_id
  AND sp.display_id IS NULL
  AND s.display_id IS NOT NULL;

-- Sync office display_id to suite_profiles (first office per suite)
UPDATE public.suite_profiles sp
SET office_display_id = o.display_id
FROM app.offices o
WHERE o.suite_id = sp.suite_id
  AND sp.office_display_id IS NULL
  AND o.display_id IS NOT NULL;

-- Trigger to keep suite_profiles.display_id in sync on suite INSERT/UPDATE
CREATE OR REPLACE FUNCTION public.trg_sync_suite_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  UPDATE public.suite_profiles
  SET display_id = NEW.display_id
  WHERE suite_id = NEW.suite_id;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_suite_display_id ON app.suites;
CREATE TRIGGER trg_sync_suite_display_id
  AFTER INSERT OR UPDATE OF display_id ON app.suites
  FOR EACH ROW EXECUTE FUNCTION public.trg_sync_suite_display_id();

-- Trigger to sync office display_id to suite_profiles on office INSERT/UPDATE
CREATE OR REPLACE FUNCTION public.trg_sync_office_display_id()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  -- Only update if suite_profiles doesn't already have an office_display_id
  UPDATE public.suite_profiles
  SET office_display_id = NEW.display_id
  WHERE suite_id = NEW.suite_id
    AND office_display_id IS NULL;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_office_display_id ON app.offices;
CREATE TRIGGER trg_sync_office_display_id
  AFTER INSERT OR UPDATE OF display_id ON app.offices
  FOR EACH ROW EXECUTE FUNCTION public.trg_sync_office_display_id();

COMMIT;
