-- =====================================================================
-- Desktop Tables Migration: Phase 0B Trust Spine Integration
-- =====================================================================
-- Purpose: Create 12 desktop-specific tables with dual-path RLS
-- Replaces: Desktop app's SQLite initDatabase() tables
-- Depends on: Trust Spine core migrations (app.suites, app.offices, receipts,
--             tenants, tenant_memberships, app.is_member)
--
-- Key Changes from Desktop SQLite:
--   1. users table → split into app.suites (Trust Spine) + suite_profiles (this migration)
--   2. user_id FK → suite_id FK throughout
--   3. TEXT columns → UUID for suite_id/office_id
--   4. receipts table → replaced by Trust Spine receipts (15-column format)
--   5. Added dual-path RLS policies on every table (Law #6: Tenant Isolation)
--
-- RLS Strategy (Dual-Path):
--   Path A: PostgREST/Supabase Auth clients → app.is_member(tenant_id) via join to app.suites
--   Path B: Express server (raw pg) → current_setting('app.current_suite_id', true)::uuid
--   Both paths enforce the same tenant isolation. OR logic ensures either path works.
--   service_role gets bypass policy for backend admin operations.
--
-- Idempotency: All operations use IF NOT EXISTS or DROP...IF EXISTS
-- =====================================================================

BEGIN;

-- =====================================================================
-- HELPER FUNCTION: Check suite access via either auth path
-- This allows clean, DRY policy definitions across all 12 tables
-- =====================================================================

CREATE OR REPLACE FUNCTION app.check_suite_access(p_suite_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT
    -- Path A: PostgREST / Supabase Auth (auth.uid() is set)
    EXISTS (
      SELECT 1 FROM app.suites s
      JOIN tenant_memberships m ON m.tenant_id = s.tenant_id
      WHERE s.suite_id = p_suite_id AND m.user_id = auth.uid()
    )
    OR
    -- Path B: Express server (app.current_suite_id is set via SET LOCAL)
    (
      current_setting('app.current_suite_id', true) IS NOT NULL
      AND p_suite_id = current_setting('app.current_suite_id', true)::uuid
    );
$$;

-- Grant execute to authenticated and service_role
GRANT EXECUTE ON FUNCTION app.check_suite_access(UUID) TO authenticated, service_role;

-- =====================================================================
-- SECTION 1: SUITE PROFILES TABLE
-- Replaces desktop users table (business profile data)
-- Links to Trust Spine app.suites table
-- =====================================================================

CREATE TABLE IF NOT EXISTS suite_profiles (
  suite_id UUID PRIMARY KEY REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  name TEXT NOT NULL,
  business_name TEXT,
  booking_slug TEXT UNIQUE,
  logo_url TEXT,
  accent_color TEXT DEFAULT '#3b82f6',
  stripe_customer_id TEXT,
  stripe_account_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_suite_profiles_booking_slug ON suite_profiles(booking_slug);
CREATE INDEX IF NOT EXISTS idx_suite_profiles_email ON suite_profiles(email);

-- RLS
ALTER TABLE suite_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE suite_profiles FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS suite_profiles_select ON suite_profiles;
CREATE POLICY suite_profiles_select ON suite_profiles
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS suite_profiles_insert ON suite_profiles;
CREATE POLICY suite_profiles_insert ON suite_profiles
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS suite_profiles_update ON suite_profiles;
CREATE POLICY suite_profiles_update ON suite_profiles
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS suite_profiles_delete_no ON suite_profiles;
CREATE POLICY suite_profiles_delete_no ON suite_profiles
  FOR DELETE TO authenticated
  USING (false);

DROP POLICY IF EXISTS suite_profiles_service_role ON suite_profiles;
CREATE POLICY suite_profiles_service_role ON suite_profiles
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE ON suite_profiles TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON suite_profiles TO service_role;

-- =====================================================================
-- SECTION 2: SERVICES TABLE
-- Bookable services/offerings
-- =====================================================================

CREATE TABLE IF NOT EXISTS services (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  duration INTEGER NOT NULL,
  price INTEGER NOT NULL,
  currency TEXT DEFAULT 'usd' NOT NULL,
  color TEXT DEFAULT '#4facfe',
  is_active BOOLEAN DEFAULT true NOT NULL,
  stripe_price_id TEXT,
  stripe_product_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_services_suite_id ON services(suite_id);
CREATE INDEX IF NOT EXISTS idx_services_suite_active ON services(suite_id, is_active);

-- RLS
ALTER TABLE services ENABLE ROW LEVEL SECURITY;
ALTER TABLE services FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS services_select ON services;
CREATE POLICY services_select ON services
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS services_insert ON services;
CREATE POLICY services_insert ON services
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS services_update ON services;
CREATE POLICY services_update ON services
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS services_delete ON services;
CREATE POLICY services_delete ON services
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS services_service_role ON services;
CREATE POLICY services_service_role ON services
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON services TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON services TO service_role;

-- =====================================================================
-- SECTION 3: BOOKINGS TABLE
-- Client appointment bookings
-- =====================================================================

CREATE TABLE IF NOT EXISTS bookings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  service_id UUID NOT NULL REFERENCES services(id),
  client_name TEXT NOT NULL,
  client_email TEXT NOT NULL,
  client_phone TEXT,
  client_notes TEXT,
  scheduled_at TIMESTAMPTZ NOT NULL,
  duration INTEGER NOT NULL,
  status TEXT DEFAULT 'pending' NOT NULL,
  payment_status TEXT DEFAULT 'unpaid' NOT NULL,
  stripe_payment_intent_id TEXT,
  stripe_checkout_session_id TEXT,
  amount INTEGER NOT NULL,
  currency TEXT DEFAULT 'usd' NOT NULL,
  cancelled_at TIMESTAMPTZ,
  cancel_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_bookings_suite_id ON bookings(suite_id);
CREATE INDEX IF NOT EXISTS idx_bookings_suite_scheduled ON bookings(suite_id, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_bookings_suite_status ON bookings(suite_id, status);

-- RLS
ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookings FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS bookings_select ON bookings;
CREATE POLICY bookings_select ON bookings
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS bookings_insert ON bookings;
CREATE POLICY bookings_insert ON bookings
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS bookings_update ON bookings;
CREATE POLICY bookings_update ON bookings
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS bookings_delete ON bookings;
CREATE POLICY bookings_delete ON bookings
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS bookings_service_role ON bookings;
CREATE POLICY bookings_service_role ON bookings
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON bookings TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON bookings TO service_role;

-- =====================================================================
-- SECTION 4: AVAILABILITY TABLE
-- Business hours/availability schedule
-- =====================================================================

CREATE TABLE IF NOT EXISTS availability (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  day_of_week INTEGER NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  is_active BOOLEAN DEFAULT true NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_availability_suite_id ON availability(suite_id);
CREATE INDEX IF NOT EXISTS idx_availability_suite_day ON availability(suite_id, day_of_week);

-- RLS
ALTER TABLE availability ENABLE ROW LEVEL SECURITY;
ALTER TABLE availability FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS availability_select ON availability;
CREATE POLICY availability_select ON availability
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS availability_insert ON availability;
CREATE POLICY availability_insert ON availability
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS availability_update ON availability;
CREATE POLICY availability_update ON availability
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS availability_delete ON availability;
CREATE POLICY availability_delete ON availability
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS availability_service_role ON availability;
CREATE POLICY availability_service_role ON availability
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON availability TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON availability TO service_role;

-- =====================================================================
-- SECTION 5: BUFFER SETTINGS TABLE
-- Booking buffer/notice settings
-- =====================================================================

CREATE TABLE IF NOT EXISTS buffer_settings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE UNIQUE,
  before_buffer INTEGER DEFAULT 0 NOT NULL,
  after_buffer INTEGER DEFAULT 15 NOT NULL,
  minimum_notice INTEGER DEFAULT 60 NOT NULL,
  max_advance_booking INTEGER DEFAULT 30 NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_buffer_settings_suite_id ON buffer_settings(suite_id);

-- RLS
ALTER TABLE buffer_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE buffer_settings FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS buffer_settings_select ON buffer_settings;
CREATE POLICY buffer_settings_select ON buffer_settings
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS buffer_settings_insert ON buffer_settings;
CREATE POLICY buffer_settings_insert ON buffer_settings
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS buffer_settings_update ON buffer_settings;
CREATE POLICY buffer_settings_update ON buffer_settings
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS buffer_settings_delete ON buffer_settings;
CREATE POLICY buffer_settings_delete ON buffer_settings
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS buffer_settings_service_role ON buffer_settings;
CREATE POLICY buffer_settings_service_role ON buffer_settings
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON buffer_settings TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON buffer_settings TO service_role;

-- =====================================================================
-- SECTION 6: FRONT DESK SETUP TABLE
-- Voice/phone system configuration
-- =====================================================================

CREATE TABLE IF NOT EXISTS front_desk_setup (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE UNIQUE,
  line_mode TEXT DEFAULT 'ASPIRE_NUMBER',
  aspire_number_e164 TEXT,
  existing_number_e164 TEXT,
  forwarding_verified BOOLEAN DEFAULT false,
  business_name TEXT,
  business_hours JSONB,
  after_hours_mode TEXT DEFAULT 'TAKE_MESSAGE',
  pronunciation TEXT,
  enabled_reasons JSONB DEFAULT '[]',
  questions_by_reason JSONB DEFAULT '{}',
  target_by_reason JSONB DEFAULT '{}',
  busy_mode TEXT DEFAULT 'TAKE_MESSAGE',
  team_members JSONB DEFAULT '[]',
  setup_complete BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_front_desk_setup_suite_id ON front_desk_setup(suite_id);

-- RLS
ALTER TABLE front_desk_setup ENABLE ROW LEVEL SECURITY;
ALTER TABLE front_desk_setup FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS front_desk_setup_select ON front_desk_setup;
CREATE POLICY front_desk_setup_select ON front_desk_setup
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS front_desk_setup_insert ON front_desk_setup;
CREATE POLICY front_desk_setup_insert ON front_desk_setup
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS front_desk_setup_update ON front_desk_setup;
CREATE POLICY front_desk_setup_update ON front_desk_setup
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS front_desk_setup_delete ON front_desk_setup;
CREATE POLICY front_desk_setup_delete ON front_desk_setup
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS front_desk_setup_service_role ON front_desk_setup;
CREATE POLICY front_desk_setup_service_role ON front_desk_setup
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON front_desk_setup TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON front_desk_setup TO service_role;

-- =====================================================================
-- SECTION 7: OAUTH TOKENS TABLE
-- OAuth provider tokens (QuickBooks, Xero, Plaid, etc.)
-- =====================================================================

CREATE TABLE IF NOT EXISTS oauth_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  access_token TEXT NOT NULL,
  refresh_token TEXT,
  realm_id TEXT,
  company_uuid TEXT,
  item_id TEXT,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(suite_id, provider)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_suite_id ON oauth_tokens(suite_id);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_suite_provider ON oauth_tokens(suite_id, provider);

-- RLS
ALTER TABLE oauth_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_tokens FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS oauth_tokens_select ON oauth_tokens;
CREATE POLICY oauth_tokens_select ON oauth_tokens
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS oauth_tokens_insert ON oauth_tokens;
CREATE POLICY oauth_tokens_insert ON oauth_tokens
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS oauth_tokens_update ON oauth_tokens;
CREATE POLICY oauth_tokens_update ON oauth_tokens
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS oauth_tokens_delete ON oauth_tokens;
CREATE POLICY oauth_tokens_delete ON oauth_tokens
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS oauth_tokens_service_role ON oauth_tokens;
CREATE POLICY oauth_tokens_service_role ON oauth_tokens
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_tokens TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_tokens TO service_role;

-- =====================================================================
-- SECTION 8: FINANCE CONNECTIONS TABLE
-- Provider connections (Stripe, QuickBooks, Plaid, etc.)
-- =====================================================================

CREATE TABLE IF NOT EXISTS finance_connections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  office_id UUID NOT NULL REFERENCES app.offices(office_id),
  provider TEXT NOT NULL,
  external_account_id TEXT,
  status TEXT DEFAULT 'connected' NOT NULL,
  scopes JSONB,
  last_sync_at TIMESTAMPTZ,
  last_webhook_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(suite_id, office_id, provider)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_finance_connections_suite_id ON finance_connections(suite_id);
CREATE INDEX IF NOT EXISTS idx_finance_connections_suite_office ON finance_connections(suite_id, office_id);
CREATE INDEX IF NOT EXISTS idx_finance_connections_suite_provider ON finance_connections(suite_id, provider);

-- RLS
ALTER TABLE finance_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_connections FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS finance_connections_select ON finance_connections;
CREATE POLICY finance_connections_select ON finance_connections
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_connections_insert ON finance_connections;
CREATE POLICY finance_connections_insert ON finance_connections
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_connections_update ON finance_connections;
CREATE POLICY finance_connections_update ON finance_connections
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_connections_delete ON finance_connections;
CREATE POLICY finance_connections_delete ON finance_connections
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_connections_service_role ON finance_connections;
CREATE POLICY finance_connections_service_role ON finance_connections
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON finance_connections TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON finance_connections TO service_role;

-- =====================================================================
-- SECTION 9: FINANCE TOKENS TABLE
-- Encrypted provider credentials (ephemeral)
-- =====================================================================

CREATE TABLE IF NOT EXISTS finance_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  connection_id UUID NOT NULL REFERENCES finance_connections(id) ON DELETE CASCADE,
  access_token_enc TEXT NOT NULL,
  refresh_token_enc TEXT,
  expires_at TIMESTAMPTZ,
  rotation_version INTEGER DEFAULT 1 NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_finance_tokens_connection_id ON finance_tokens(connection_id);

-- RLS (join through finance_connections to get suite_id)
ALTER TABLE finance_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_tokens FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS finance_tokens_select ON finance_tokens;
CREATE POLICY finance_tokens_select ON finance_tokens
  FOR SELECT TO authenticated
  USING (
    connection_id IN (
      SELECT id FROM finance_connections WHERE app.check_suite_access(suite_id)
    )
  );

DROP POLICY IF EXISTS finance_tokens_insert ON finance_tokens;
CREATE POLICY finance_tokens_insert ON finance_tokens
  FOR INSERT TO authenticated
  WITH CHECK (
    connection_id IN (
      SELECT id FROM finance_connections WHERE app.check_suite_access(suite_id)
    )
  );

DROP POLICY IF EXISTS finance_tokens_update ON finance_tokens;
CREATE POLICY finance_tokens_update ON finance_tokens
  FOR UPDATE TO authenticated
  USING (
    connection_id IN (
      SELECT id FROM finance_connections WHERE app.check_suite_access(suite_id)
    )
  )
  WITH CHECK (
    connection_id IN (
      SELECT id FROM finance_connections WHERE app.check_suite_access(suite_id)
    )
  );

DROP POLICY IF EXISTS finance_tokens_delete ON finance_tokens;
CREATE POLICY finance_tokens_delete ON finance_tokens
  FOR DELETE TO authenticated
  USING (
    connection_id IN (
      SELECT id FROM finance_connections WHERE app.check_suite_access(suite_id)
    )
  );

DROP POLICY IF EXISTS finance_tokens_service_role ON finance_tokens;
CREATE POLICY finance_tokens_service_role ON finance_tokens
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON finance_tokens TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON finance_tokens TO service_role;

-- =====================================================================
-- SECTION 10: FINANCE EVENTS TABLE
-- Financial transactions/events (invoices, payments, charges)
-- =====================================================================

CREATE TABLE IF NOT EXISTS finance_events (
  event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  office_id UUID NOT NULL REFERENCES app.offices(office_id),
  connection_id UUID REFERENCES finance_connections(id),
  provider TEXT NOT NULL,
  provider_event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  amount INTEGER,
  currency TEXT DEFAULT 'usd',
  status TEXT DEFAULT 'posted',
  entity_refs JSONB,
  raw_hash TEXT,
  receipt_id TEXT REFERENCES receipts(receipt_id),
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(suite_id, office_id, provider, provider_event_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_finance_events_suite_id ON finance_events(suite_id);
CREATE INDEX IF NOT EXISTS idx_finance_events_suite_office ON finance_events(suite_id, office_id);
CREATE INDEX IF NOT EXISTS idx_finance_events_suite_office_occurred ON finance_events(suite_id, office_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_finance_events_provider ON finance_events(provider);
CREATE INDEX IF NOT EXISTS idx_finance_events_receipt_id ON finance_events(receipt_id);

-- RLS
ALTER TABLE finance_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS finance_events_select ON finance_events;
CREATE POLICY finance_events_select ON finance_events
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_events_insert ON finance_events;
CREATE POLICY finance_events_insert ON finance_events
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_events_update ON finance_events;
CREATE POLICY finance_events_update ON finance_events
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

-- Finance events are append-only (no delete for authenticated)
DROP POLICY IF EXISTS finance_events_delete_no ON finance_events;
CREATE POLICY finance_events_delete_no ON finance_events
  FOR DELETE TO authenticated
  USING (false);

DROP POLICY IF EXISTS finance_events_service_role ON finance_events;
CREATE POLICY finance_events_service_role ON finance_events
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE ON finance_events TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON finance_events TO service_role;

-- =====================================================================
-- SECTION 11: FINANCE ENTITIES TABLE
-- Cached provider entities (customers, products, accounts)
-- =====================================================================

CREATE TABLE IF NOT EXISTS finance_entities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  office_id UUID NOT NULL REFERENCES app.offices(office_id),
  connection_id UUID REFERENCES finance_connections(id),
  provider TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  data JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_finance_entities_suite_id ON finance_entities(suite_id);
CREATE INDEX IF NOT EXISTS idx_finance_entities_suite_office ON finance_entities(suite_id, office_id);
CREATE INDEX IF NOT EXISTS idx_finance_entities_provider_type ON finance_entities(provider, entity_type);
CREATE INDEX IF NOT EXISTS idx_finance_entities_entity_id ON finance_entities(entity_id);

-- RLS
ALTER TABLE finance_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_entities FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS finance_entities_select ON finance_entities;
CREATE POLICY finance_entities_select ON finance_entities
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_entities_insert ON finance_entities;
CREATE POLICY finance_entities_insert ON finance_entities
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_entities_update ON finance_entities;
CREATE POLICY finance_entities_update ON finance_entities
  FOR UPDATE TO authenticated
  USING (app.check_suite_access(suite_id))
  WITH CHECK (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_entities_delete ON finance_entities;
CREATE POLICY finance_entities_delete ON finance_entities
  FOR DELETE TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_entities_service_role ON finance_entities;
CREATE POLICY finance_entities_service_role ON finance_entities
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON finance_entities TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON finance_entities TO service_role;

-- =====================================================================
-- SECTION 12: FINANCE SNAPSHOTS TABLE
-- 5-Chapter financial summaries (append-only)
-- =====================================================================

CREATE TABLE IF NOT EXISTS finance_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  office_id UUID NOT NULL REFERENCES app.offices(office_id),
  generated_at TIMESTAMPTZ NOT NULL,
  chapter_now JSONB,
  chapter_next JSONB,
  chapter_month JSONB,
  chapter_reconcile JSONB,
  chapter_actions JSONB,
  sources JSONB,
  staleness JSONB,
  receipt_id TEXT REFERENCES receipts(receipt_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_finance_snapshots_suite_id ON finance_snapshots(suite_id);
CREATE INDEX IF NOT EXISTS idx_finance_snapshots_suite_office ON finance_snapshots(suite_id, office_id);
CREATE INDEX IF NOT EXISTS idx_finance_snapshots_generated_at ON finance_snapshots(generated_at);

-- RLS
ALTER TABLE finance_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_snapshots FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS finance_snapshots_select ON finance_snapshots;
CREATE POLICY finance_snapshots_select ON finance_snapshots
  FOR SELECT TO authenticated
  USING (app.check_suite_access(suite_id));

DROP POLICY IF EXISTS finance_snapshots_insert ON finance_snapshots;
CREATE POLICY finance_snapshots_insert ON finance_snapshots
  FOR INSERT TO authenticated
  WITH CHECK (app.check_suite_access(suite_id));

-- Snapshots are append-only (no update/delete for authenticated)
DROP POLICY IF EXISTS finance_snapshots_update_no ON finance_snapshots;
CREATE POLICY finance_snapshots_update_no ON finance_snapshots
  FOR UPDATE TO authenticated
  USING (false);

DROP POLICY IF EXISTS finance_snapshots_delete_no ON finance_snapshots;
CREATE POLICY finance_snapshots_delete_no ON finance_snapshots
  FOR DELETE TO authenticated
  USING (false);

DROP POLICY IF EXISTS finance_snapshots_service_role ON finance_snapshots;
CREATE POLICY finance_snapshots_service_role ON finance_snapshots
  TO service_role
  USING (true)
  WITH CHECK (true);

-- Grants
GRANT SELECT, INSERT ON finance_snapshots TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON finance_snapshots TO service_role;

-- =====================================================================
-- SECTION 13: UPDATED_AT TRIGGER FUNCTION
-- Auto-update updated_at column on row modification
-- =====================================================================

CREATE OR REPLACE FUNCTION app.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- Apply to tables with updated_at
DO $$
DECLARE
  tbl TEXT;
BEGIN
  FOR tbl IN SELECT unnest(ARRAY[
    'suite_profiles', 'services', 'bookings', 'front_desk_setup',
    'oauth_tokens', 'finance_connections', 'finance_tokens', 'finance_entities'
  ])
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS set_updated_at ON %I; CREATE TRIGGER set_updated_at BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();',
      tbl, tbl
    );
  END LOOP;
END;
$$;

-- =====================================================================
-- SECTION 14: COMPLETION
-- =====================================================================

COMMIT;

-- Migration complete: 12 desktop tables + 1 helper function + updated_at triggers
-- Dual-path RLS: auth.uid() for PostgREST + current_setting for Express server
-- Next step: Apply Trust Spine core migrations first, then this migration
