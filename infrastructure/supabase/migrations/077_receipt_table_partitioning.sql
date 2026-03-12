-- Migration 077: Receipt Table Partitioning (Phase 3D)
-- Creates a partitioned receipts table for query performance at scale.
-- Unblocks 7K+ concurrent users by partitioning by month.
--
-- STRATEGY: Create parallel partitioned table, DO NOT swap yet.
-- Data migration happens separately with a maintenance script.
-- This is non-destructive — the original receipts table is untouched.
--
-- Future swap procedure (run manually during maintenance window):
--   INSERT INTO receipts_partitioned SELECT * FROM receipts;
--   ALTER TABLE receipts RENAME TO receipts_old;
--   ALTER TABLE receipts_partitioned RENAME TO receipts;

-- ============================================================================
-- 1. Create partitioned table (mirrors receipts schema)
-- ============================================================================

CREATE TABLE IF NOT EXISTS receipts_partitioned (
    id uuid DEFAULT gen_random_uuid(),
    receipt_id text NOT NULL,
    suite_id uuid NOT NULL,
    correlation_id text,
    action_type text NOT NULL,
    risk_tier text DEFAULT 'GREEN',
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'DENIED')),
    actor_id uuid,
    actor_type text DEFAULT 'system',
    office_id uuid,
    tool_name text,
    inputs jsonb DEFAULT '{}',
    outputs jsonb DEFAULT '{}',
    metadata jsonb DEFAULT '{}',
    policy jsonb DEFAULT '{}',
    error_detail text,
    display_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Apply display_id trigger (from migration 063) to partitioned table
DROP TRIGGER IF EXISTS trg_receipt_display_id ON receipts_partitioned;
CREATE TRIGGER trg_receipt_display_id
  BEFORE INSERT ON receipts_partitioned
  FOR EACH ROW EXECUTE FUNCTION public.trg_receipt_display_id();

-- display_id index
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_part_display_id
    ON receipts_partitioned (suite_id, display_id) WHERE display_id IS NOT NULL;

-- ============================================================================
-- 2. Create monthly partitions (current month + next 3)
-- ============================================================================

DO $$
DECLARE
    start_date date;
    end_date date;
    partition_name text;
BEGIN
    FOR i IN 0..3 LOOP
        start_date := date_trunc('month', CURRENT_DATE) + (i || ' months')::interval;
        end_date := start_date + '1 month'::interval;
        partition_name := 'receipts_p_' || to_char(start_date, 'YYYY_MM');

        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF receipts_partitioned
             FOR VALUES FROM (%L) TO (%L)',
            partition_name, start_date, end_date
        );
    END LOOP;
END $$;

-- ============================================================================
-- 3. Default partition for data outside defined ranges
-- ============================================================================

CREATE TABLE IF NOT EXISTS receipts_p_default PARTITION OF receipts_partitioned DEFAULT;

-- ============================================================================
-- 4. Replicate indexes from original receipts table
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_receipts_part_suite_created
    ON receipts_partitioned (suite_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_receipts_part_correlation
    ON receipts_partitioned (correlation_id) WHERE correlation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_receipts_part_receipt_id
    ON receipts_partitioned (receipt_id);

CREATE INDEX IF NOT EXISTS idx_receipts_part_action_type
    ON receipts_partitioned (action_type);

-- ============================================================================
-- 5. Enable RLS (matching original receipts table — Law #6)
-- ============================================================================

ALTER TABLE receipts_partitioned ENABLE ROW LEVEL SECURITY;
ALTER TABLE receipts_partitioned FORCE ROW LEVEL SECURITY;

-- Tenant isolation: users see only their own suite's receipts
CREATE POLICY receipts_part_tenant_select ON receipts_partitioned
    FOR SELECT USING (
        suite_id IN (
            SELECT tm.suite_id FROM tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

-- Service role bypass for backend operations
CREATE POLICY receipts_part_service_role ON receipts_partitioned
    FOR ALL USING (
        current_setting('role', true) = 'service_role'
    );

-- Append-only: authenticated users can INSERT but NOT update/delete
CREATE POLICY receipts_part_insert_only ON receipts_partitioned
    FOR INSERT WITH CHECK (
        suite_id IN (
            SELECT tm.suite_id FROM tenant_memberships tm
            WHERE tm.user_id = auth.uid()
        )
    );

-- ============================================================================
-- 6. Auto-partition creation function (called monthly by pg_cron or edge function)
-- ============================================================================

CREATE OR REPLACE FUNCTION app.create_receipt_partitions(months_ahead int DEFAULT 3)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    start_date date;
    end_date date;
    partition_name text;
BEGIN
    FOR i IN 0..months_ahead LOOP
        start_date := date_trunc('month', CURRENT_DATE) + (i || ' months')::interval;
        end_date := start_date + '1 month'::interval;
        partition_name := 'receipts_p_' || to_char(start_date, 'YYYY_MM');

        -- Skip if partition already exists
        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relname = partition_name
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF receipts_partitioned
                 FOR VALUES FROM (%L) TO (%L)',
                partition_name, start_date, end_date
            );
            RAISE NOTICE 'Created partition: %', partition_name;
        END IF;
    END LOOP;
END;
$$;

GRANT EXECUTE ON FUNCTION app.create_receipt_partitions(int) TO service_role;

-- ============================================================================
-- NOTE: Data migration from `receipts` to `receipts_partitioned` is a separate
-- maintenance operation. The swap procedure:
--   1. INSERT INTO receipts_partitioned SELECT * FROM receipts;
--   2. ALTER TABLE receipts RENAME TO receipts_old;
--   3. ALTER TABLE receipts_partitioned RENAME TO receipts;
--   4. Re-apply display_id trigger on new receipts table
--   5. Verify RLS policies and run evil tests
-- ============================================================================
