-- Phase 3 W10: Enterprise intake fields for suite_profiles + founder_hub_notes
-- Supports the expanded onboarding wizard with address, business context, consent

-- ═══════════════════════════════════════════════════════════════════
-- 1. Personal identity (for Ava personalization)
-- ═══════════════════════════════════════════════════════════════════
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS date_of_birth DATE;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS gender TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS role_category TEXT;

-- ═══════════════════════════════════════════════════════════════════
-- 2. Home address
-- ═══════════════════════════════════════════════════════════════════
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS home_address_line1 TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS home_address_line2 TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS home_city TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS home_state TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS home_zip TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS home_country TEXT DEFAULT 'US';

-- ═══════════════════════════════════════════════════════════════════
-- 3. Business address (can be "same as home")
-- ═══════════════════════════════════════════════════════════════════
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS business_address_same_as_home BOOLEAN DEFAULT true;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS business_address_line1 TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS business_address_line2 TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS business_city TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS business_state TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS business_zip TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS business_country TEXT DEFAULT 'US';

-- ═══════════════════════════════════════════════════════════════════
-- 4. Business context
-- ═══════════════════════════════════════════════════════════════════
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS entity_type TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS years_in_business TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS sales_channel TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS customer_type TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS annual_revenue_band TEXT;

-- ═══════════════════════════════════════════════════════════════════
-- 5. Services & priorities
-- ═══════════════════════════════════════════════════════════════════
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS services_timeline JSONB DEFAULT '{}';
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS services_priority TEXT[] DEFAULT '{}';
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS tools_planning TEXT[] DEFAULT '{}';
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS business_goals TEXT[] DEFAULT '{}';

-- ═══════════════════════════════════════════════════════════════════
-- 6. Preferences
-- ═══════════════════════════════════════════════════════════════════
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS preferred_channel TEXT DEFAULT 'warm';
-- timezone and currency already exist from TenantProvider defaults
-- but ensure they exist as actual columns
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS timezone TEXT;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'USD';
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS fiscal_year_end_month INT;

-- ═══════════════════════════════════════════════════════════════════
-- 7. Consent & governance
-- ═══════════════════════════════════════════════════════════════════
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS consent_personalization BOOLEAN DEFAULT false;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS consent_communications BOOLEAN DEFAULT false;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS intake_schema_version INT DEFAULT 1;
ALTER TABLE suite_profiles ADD COLUMN IF NOT EXISTS intake_receipt_id UUID;

-- ═══════════════════════════════════════════════════════════════════
-- 8. CHECK constraints on enum columns
-- ═══════════════════════════════════════════════════════════════════
DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_gender
    CHECK (gender IN ('male','female','non-binary','prefer-not-to-say') OR gender IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_entity_type
    CHECK (entity_type IN ('sole_proprietorship','llc','s_corp','c_corp','partnership','nonprofit','other') OR entity_type IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_preferred_channel
    CHECK (preferred_channel IN ('cold','warm','hot') OR preferred_channel IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_fiscal_year_end_month
    CHECK (fiscal_year_end_month BETWEEN 1 AND 12 OR fiscal_year_end_month IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_currency
    CHECK (currency ~ '^[A-Z]{3}$' OR currency IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_years_in_business
    CHECK (years_in_business IN ('less_than_1','1_to_3','3_to_5','5_to_10','10_plus') OR years_in_business IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_sales_channel
    CHECK (sales_channel IN ('online','in_person','both','other') OR sales_channel IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE suite_profiles ADD CONSTRAINT chk_sp_customer_type
    CHECK (customer_type IN ('b2b','b2c','both') OR customer_type IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ═══════════════════════════════════════════════════════════════════
-- 9. Founder Hub Notes table (for notes.tsx CRUD)
-- ═══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS founder_hub_notes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL REFERENCES suites(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  pinned BOOLEAN DEFAULT false,
  tags TEXT[] DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE founder_hub_notes ENABLE ROW LEVEL SECURITY;

-- RLS: tenant isolation (Law #6)
DO $$ BEGIN
  CREATE POLICY notes_tenant_select ON founder_hub_notes
    FOR SELECT USING (suite_id = current_setting('app.current_suite_id')::uuid);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE POLICY notes_tenant_insert ON founder_hub_notes
    FOR INSERT WITH CHECK (suite_id = current_setting('app.current_suite_id')::uuid);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE POLICY notes_tenant_update ON founder_hub_notes
    FOR UPDATE USING (suite_id = current_setting('app.current_suite_id')::uuid);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE POLICY notes_tenant_delete ON founder_hub_notes
    FOR DELETE USING (suite_id = current_setting('app.current_suite_id')::uuid);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_fh_notes_suite_id ON founder_hub_notes(suite_id);
CREATE INDEX IF NOT EXISTS idx_fh_notes_updated ON founder_hub_notes(suite_id, updated_at DESC);

-- ═══════════════════════════════════════════════════════════════════
-- 10. Indexes on new suite_profiles columns for common queries
-- ═══════════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_sp_industry ON suite_profiles(industry) WHERE industry IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sp_onboarding_consent ON suite_profiles(onboarding_completed_at, consent_personalization)
  WHERE onboarding_completed_at IS NOT NULL AND consent_personalization = true;
