-- =====================================================================
-- Migration 052: Front Desk Enterprise Telephony + SMS + Voicemail
-- =====================================================================
-- Consolidated from enterprise pack migrations 000-012.
-- Tables are in public schema (frontdesk-scoped).
-- RLS uses public.current_suite_id() reading from app.current_suite_id
-- setting (set by Express server via SET LOCAL).
--
-- Governance Compliance:
--   Law #2: All state changes produce receipts (frontdesk_action_receipts + frontdesk_call_receipts)
--   Law #3: Fail closed — RLS denies by default, SMS blocked by default (sms_enabled=false)
--   Law #6: Tenant isolation — suite_id FK on every table, ENABLE + FORCE RLS
--   Law #7: Tools are hands — outbox pattern ensures orchestrator decides
--
-- Idempotency: All operations use IF NOT EXISTS / DROP ... IF EXISTS
-- =====================================================================

BEGIN;

-- =====================================================================
-- SECTION 0: AUTH HELPERS (idempotent)
-- Reads suite_id from app.current_suite_id setting (set by Express middleware).
-- This bridges the enterprise pack's RLS pattern to our server's set_config approach.
-- =====================================================================

CREATE OR REPLACE FUNCTION public.current_suite_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
  SELECT nullif(current_setting('app.current_suite_id', true), '')::uuid;
$$;

CREATE OR REPLACE FUNCTION public.current_office_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
  SELECT nullif(current_setting('app.current_office_id', true), '')::uuid;
$$;

-- =====================================================================
-- SECTION 1: ENUMS
-- =====================================================================

DO $$ BEGIN
  CREATE TYPE frontdesk_line_mode AS ENUM ('ASPIRE_FULL_DUPLEX', 'EXISTING_INBOUND_ONLY');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE frontdesk_call_direction AS ENUM ('inbound', 'outbound');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE frontdesk_call_status AS ENUM ('ringing', 'in_progress', 'completed', 'failed', 'voicemail', 'blocked');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE frontdesk_message_direction AS ENUM ('inbound', 'outbound');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE frontdesk_resource_status AS ENUM ('provisioning', 'active', 'releasing', 'released', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE frontdesk_transcript_status AS ENUM ('pending', 'complete', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE frontdesk_a2p_status AS ENUM ('unknown', 'pending', 'approved', 'rejected', 'not_required');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- =====================================================================
-- SECTION 2: TABLES (in FK dependency order)
-- =====================================================================

-- 2a. business_lines — line config per suite
CREATE TABLE IF NOT EXISTS public.business_lines (
  business_line_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id             UUID NOT NULL,
  owner_office_id      UUID NOT NULL,
  line_mode            frontdesk_line_mode NOT NULL DEFAULT 'ASPIRE_FULL_DUPLEX',
  business_number      TEXT,
  existing_number      TEXT,
  country              TEXT DEFAULT 'US',
  -- Extended setup fields (from front_desk_setup)
  business_name        TEXT,
  business_hours       JSONB DEFAULT '{}'::jsonb,
  after_hours_mode     TEXT DEFAULT 'TAKE_MESSAGE',
  pronunciation        TEXT,
  enabled_reasons      JSONB DEFAULT '[]'::jsonb,
  questions_by_reason  JSONB DEFAULT '{}'::jsonb,
  target_by_reason     JSONB DEFAULT '{}'::jsonb,
  busy_mode            TEXT DEFAULT 'TAKE_MESSAGE',
  team_members         JSONB DEFAULT '[]'::jsonb,
  setup_complete       BOOLEAN DEFAULT FALSE,
  greeting_voice_id    TEXT DEFAULT 'DODLEQrClDo8wCz460ld',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_business_lines_suite ON public.business_lines (suite_id);
CREATE INDEX IF NOT EXISTS idx_business_lines_owner ON public.business_lines (suite_id, owner_office_id);

ALTER TABLE public.business_lines ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS business_lines_select ON public.business_lines;
CREATE POLICY business_lines_select ON public.business_lines FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS business_lines_insert ON public.business_lines;
CREATE POLICY business_lines_insert ON public.business_lines FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS business_lines_update ON public.business_lines;
CREATE POLICY business_lines_update ON public.business_lines FOR UPDATE
  USING (suite_id = public.current_suite_id())
  WITH CHECK (suite_id = public.current_suite_id());


-- 2b. extensions — phone extensions per business line
CREATE TABLE IF NOT EXISTS public.extensions (
  extension_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id           UUID NOT NULL,
  business_line_id   UUID NOT NULL REFERENCES public.business_lines(business_line_id) ON DELETE CASCADE,
  office_id          UUID NOT NULL,
  extension_number   TEXT NOT NULL,
  display_name       TEXT,
  routing_rules      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (suite_id, business_line_id, extension_number)
);

CREATE INDEX IF NOT EXISTS idx_extensions_suite ON public.extensions (suite_id);
CREATE INDEX IF NOT EXISTS idx_extensions_line ON public.extensions (suite_id, business_line_id);

ALTER TABLE public.extensions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS extensions_select ON public.extensions;
CREATE POLICY extensions_select ON public.extensions FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS extensions_insert ON public.extensions;
CREATE POLICY extensions_insert ON public.extensions FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS extensions_update ON public.extensions;
CREATE POLICY extensions_update ON public.extensions FOR UPDATE
  USING (suite_id = public.current_suite_id())
  WITH CHECK (suite_id = public.current_suite_id());


-- 2c. call_sessions — call tracking with provider IDs + idempotency
CREATE TABLE IF NOT EXISTS public.call_sessions (
  call_session_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id           UUID NOT NULL,
  business_line_id   UUID NOT NULL REFERENCES public.business_lines(business_line_id) ON DELETE RESTRICT,
  owner_office_id    UUID NOT NULL,
  direction          frontdesk_call_direction NOT NULL,
  status             frontdesk_call_status NOT NULL DEFAULT 'ringing',
  from_number        TEXT,
  to_number          TEXT,
  caller_name        TEXT,
  duration_seconds   INT,
  provider           TEXT NOT NULL,
  provider_call_id   TEXT NOT NULL,
  provider_event_id  TEXT,
  started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at           TIMESTAMPTZ,
  recording_url      TEXT,
  voicemail_url      TEXT,
  metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (provider, provider_call_id)
);

CREATE INDEX IF NOT EXISTS idx_call_sessions_suite ON public.call_sessions (suite_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_sessions_line ON public.call_sessions (suite_id, business_line_id, started_at DESC);

ALTER TABLE public.call_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS call_sessions_select ON public.call_sessions;
CREATE POLICY call_sessions_select ON public.call_sessions FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS call_sessions_insert ON public.call_sessions;
CREATE POLICY call_sessions_insert ON public.call_sessions FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS call_sessions_update ON public.call_sessions;
CREATE POLICY call_sessions_update ON public.call_sessions FOR UPDATE
  USING (suite_id = public.current_suite_id())
  WITH CHECK (suite_id = public.current_suite_id());


-- 2d. frontdesk_webhook_events — idempotency for webhook replay protection
CREATE TABLE IF NOT EXISTS public.frontdesk_webhook_events (
  webhook_event_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id           UUID NOT NULL,
  provider           TEXT NOT NULL,
  provider_event_id  TEXT NOT NULL,
  provider_call_id   TEXT,
  event_type         TEXT,
  received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  raw_headers        JSONB NOT NULL DEFAULT '{}'::jsonb,
  raw_body           TEXT,
  UNIQUE (provider, provider_event_id)
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_suite ON public.frontdesk_webhook_events (suite_id, received_at DESC);

ALTER TABLE public.frontdesk_webhook_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS webhook_events_select ON public.frontdesk_webhook_events;
CREATE POLICY webhook_events_select ON public.frontdesk_webhook_events FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS webhook_events_insert ON public.frontdesk_webhook_events;
CREATE POLICY webhook_events_insert ON public.frontdesk_webhook_events FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());


-- 2e. frontdesk_call_receipts — call artifacts, optional FK to Trust Spine
CREATE TABLE IF NOT EXISTS public.frontdesk_call_receipts (
  frontdesk_call_receipt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id                  UUID NOT NULL,
  call_session_id           UUID NOT NULL REFERENCES public.call_sessions(call_session_id) ON DELETE CASCADE,
  trust_receipt_id          UUID,
  actor_type                TEXT NOT NULL DEFAULT 'agent',
  actor_id                  UUID,
  transcript_ref            TEXT,
  recording_ref             TEXT,
  summary                   JSONB NOT NULL DEFAULT '{}'::jsonb,
  outcome                   TEXT,
  next_step                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_call_receipts_suite ON public.frontdesk_call_receipts (suite_id, created_at DESC);

ALTER TABLE public.frontdesk_call_receipts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS call_receipts_select ON public.frontdesk_call_receipts;
CREATE POLICY call_receipts_select ON public.frontdesk_call_receipts FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS call_receipts_insert ON public.frontdesk_call_receipts;
CREATE POLICY call_receipts_insert ON public.frontdesk_call_receipts FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());


-- 2f. frontdesk_usage_monthly — metering counters
CREATE TABLE IF NOT EXISTS public.frontdesk_usage_monthly (
  usage_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id    UUID NOT NULL,
  office_id   UUID,
  month_start DATE NOT NULL,
  inbound_ai_minutes        INT NOT NULL DEFAULT 0,
  outbound_callback_minutes INT NOT NULL DEFAULT 0,
  sms_segments              INT NOT NULL DEFAULT 0,
  recording_storage_bytes   BIGINT NOT NULL DEFAULT 0,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (suite_id, office_id, month_start)
);

CREATE INDEX IF NOT EXISTS idx_usage_suite_month ON public.frontdesk_usage_monthly (suite_id, month_start DESC);

ALTER TABLE public.frontdesk_usage_monthly ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS usage_select ON public.frontdesk_usage_monthly;
CREATE POLICY usage_select ON public.frontdesk_usage_monthly FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS usage_insert ON public.frontdesk_usage_monthly;
CREATE POLICY usage_insert ON public.frontdesk_usage_monthly FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS usage_update ON public.frontdesk_usage_monthly;
CREATE POLICY usage_update ON public.frontdesk_usage_monthly FOR UPDATE
  USING (suite_id = public.current_suite_id())
  WITH CHECK (suite_id = public.current_suite_id());


-- 2g. frontdesk_provider_resources — Twilio number lifecycle
CREATE TABLE IF NOT EXISTS public.frontdesk_provider_resources (
  provider_resource_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id                        UUID NOT NULL,
  business_line_id                UUID NOT NULL REFERENCES public.business_lines(business_line_id) ON DELETE CASCADE,
  provider                        TEXT NOT NULL DEFAULT 'twilio',
  business_number_e164            TEXT NOT NULL,
  twilio_incoming_phone_number_sid TEXT,
  twilio_trunk_sid                TEXT,
  status                          frontdesk_resource_status NOT NULL DEFAULT 'provisioning',
  last_error                      TEXT,
  created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
  released_at                     TIMESTAMPTZ,
  UNIQUE (provider, business_number_e164),
  UNIQUE (suite_id, business_line_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_resources_suite ON public.frontdesk_provider_resources (suite_id, created_at DESC);

ALTER TABLE public.frontdesk_provider_resources ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS provider_resources_select ON public.frontdesk_provider_resources;
CREATE POLICY provider_resources_select ON public.frontdesk_provider_resources FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS provider_resources_insert ON public.frontdesk_provider_resources;
CREATE POLICY provider_resources_insert ON public.frontdesk_provider_resources FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());


-- 2h. frontdesk_sms_threads — SMS conversations
CREATE TABLE IF NOT EXISTS public.frontdesk_sms_threads (
  thread_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id            UUID NOT NULL,
  owner_office_id     UUID NOT NULL,
  business_line_id    UUID NOT NULL REFERENCES public.business_lines(business_line_id) ON DELETE CASCADE,
  business_number_e164 TEXT NOT NULL,
  counterparty_e164   TEXT NOT NULL,
  last_message_at     TIMESTAMPTZ,
  unread_count        INT NOT NULL DEFAULT 0,
  status              TEXT NOT NULL DEFAULT 'active',
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (suite_id, business_number_e164, counterparty_e164)
);

CREATE INDEX IF NOT EXISTS idx_sms_threads_suite ON public.frontdesk_sms_threads (suite_id, last_message_at DESC);

ALTER TABLE public.frontdesk_sms_threads ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sms_threads_select ON public.frontdesk_sms_threads;
CREATE POLICY sms_threads_select ON public.frontdesk_sms_threads FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS sms_threads_insert ON public.frontdesk_sms_threads;
CREATE POLICY sms_threads_insert ON public.frontdesk_sms_threads FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS sms_threads_update ON public.frontdesk_sms_threads;
CREATE POLICY sms_threads_update ON public.frontdesk_sms_threads FOR UPDATE
  USING (suite_id = public.current_suite_id())
  WITH CHECK (suite_id = public.current_suite_id());


-- 2i. frontdesk_sms_messages — individual messages
CREATE TABLE IF NOT EXISTS public.frontdesk_sms_messages (
  sms_message_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id             UUID NOT NULL,
  thread_id            UUID NOT NULL REFERENCES public.frontdesk_sms_threads(thread_id) ON DELETE CASCADE,
  direction            frontdesk_message_direction NOT NULL,
  body                 TEXT NOT NULL,
  num_segments         INT,
  media_count          INT NOT NULL DEFAULT 0,
  media_urls           JSONB NOT NULL DEFAULT '[]'::jsonb,
  delivery_status      TEXT,
  provider             TEXT NOT NULL DEFAULT 'twilio',
  provider_message_sid TEXT,
  received_at          TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (provider, provider_message_sid, suite_id)
);

CREATE INDEX IF NOT EXISTS idx_sms_messages_thread ON public.frontdesk_sms_messages (thread_id, created_at ASC);

ALTER TABLE public.frontdesk_sms_messages ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sms_messages_select ON public.frontdesk_sms_messages;
CREATE POLICY sms_messages_select ON public.frontdesk_sms_messages FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS sms_messages_insert ON public.frontdesk_sms_messages;
CREATE POLICY sms_messages_insert ON public.frontdesk_sms_messages FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());


-- 2j. frontdesk_sms_opt_outs — opt-out tracking
CREATE TABLE IF NOT EXISTS public.frontdesk_sms_opt_outs (
  opt_out_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id             UUID NOT NULL,
  business_number_e164 TEXT NOT NULL,
  counterparty_e164    TEXT NOT NULL,
  opted_out_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  source               TEXT NOT NULL DEFAULT 'twilio',
  UNIQUE (suite_id, business_number_e164, counterparty_e164)
);

ALTER TABLE public.frontdesk_sms_opt_outs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sms_opt_outs_select ON public.frontdesk_sms_opt_outs;
CREATE POLICY sms_opt_outs_select ON public.frontdesk_sms_opt_outs FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS sms_opt_outs_insert ON public.frontdesk_sms_opt_outs;
CREATE POLICY sms_opt_outs_insert ON public.frontdesk_sms_opt_outs FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());


-- 2k. frontdesk_voicemails — voicemail artifacts + transcription
CREATE TABLE IF NOT EXISTS public.frontdesk_voicemails (
  voicemail_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id           UUID NOT NULL,
  business_line_id   UUID NOT NULL REFERENCES public.business_lines(business_line_id) ON DELETE RESTRICT,
  owner_office_id    UUID NOT NULL,
  call_session_id    UUID REFERENCES public.call_sessions(call_session_id) ON DELETE SET NULL,
  from_e164          TEXT,
  to_e164            TEXT,
  duration_seconds   INT,
  recording_uri      TEXT,
  recording_bytes    BIGINT NOT NULL DEFAULT 0,
  transcript_status  frontdesk_transcript_status NOT NULL DEFAULT 'pending',
  transcript_text    TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_voicemails_suite ON public.frontdesk_voicemails (suite_id, created_at DESC);

ALTER TABLE public.frontdesk_voicemails ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS voicemails_select ON public.frontdesk_voicemails;
CREATE POLICY voicemails_select ON public.frontdesk_voicemails FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS voicemails_insert ON public.frontdesk_voicemails;
CREATE POLICY voicemails_insert ON public.frontdesk_voicemails FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());


-- 2l. frontdesk_messaging_compliance — SMS compliance gates (fail-closed defaults)
CREATE TABLE IF NOT EXISTS public.frontdesk_messaging_compliance (
  compliance_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id         UUID NOT NULL UNIQUE,
  sms_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
  a2p_10dlc_status frontdesk_a2p_status NOT NULL DEFAULT 'unknown',
  a2p_brand_sid    TEXT,
  a2p_campaign_sid TEXT,
  last_error       TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.frontdesk_messaging_compliance ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS messaging_compliance_select ON public.frontdesk_messaging_compliance;
CREATE POLICY messaging_compliance_select ON public.frontdesk_messaging_compliance FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS messaging_compliance_insert ON public.frontdesk_messaging_compliance;
CREATE POLICY messaging_compliance_insert ON public.frontdesk_messaging_compliance FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());


-- 2m. frontdesk_outbox_jobs — outbox-first job queue
CREATE TABLE IF NOT EXISTS public.frontdesk_outbox_jobs (
  job_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id         UUID NOT NULL,
  job_type         TEXT NOT NULL,
  idempotency_key  TEXT NOT NULL,
  payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
  status           TEXT NOT NULL DEFAULT 'pending',
  run_after        TIMESTAMPTZ,
  attempts         INT NOT NULL DEFAULT 0,
  max_attempts     INT NOT NULL DEFAULT 12,
  locked_at        TIMESTAMPTZ,
  locked_by        TEXT,
  last_error       TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (suite_id, job_type, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_frontdesk_outbox_pending
  ON public.frontdesk_outbox_jobs (status, run_after, created_at);

ALTER TABLE public.frontdesk_outbox_jobs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS frontdesk_outbox_select ON public.frontdesk_outbox_jobs;
CREATE POLICY frontdesk_outbox_select ON public.frontdesk_outbox_jobs FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS frontdesk_outbox_insert ON public.frontdesk_outbox_jobs;
CREATE POLICY frontdesk_outbox_insert ON public.frontdesk_outbox_jobs FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS frontdesk_outbox_update ON public.frontdesk_outbox_jobs;
CREATE POLICY frontdesk_outbox_update ON public.frontdesk_outbox_jobs FOR UPDATE
  USING (suite_id = public.current_suite_id())
  WITH CHECK (suite_id = public.current_suite_id());


-- 2n. frontdesk_action_receipts — action receipts for gateway responses
CREATE TABLE IF NOT EXISTS public.frontdesk_action_receipts (
  receipt_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id        UUID NOT NULL,
  actor_type      TEXT NOT NULL DEFAULT 'system',
  actor_id        UUID,
  action_type     TEXT NOT NULL,
  correlation_id  TEXT,
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_frontdesk_action_receipts_suite
  ON public.frontdesk_action_receipts (suite_id, created_at DESC);

ALTER TABLE public.frontdesk_action_receipts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS action_receipts_select ON public.frontdesk_action_receipts;
CREATE POLICY action_receipts_select ON public.frontdesk_action_receipts FOR SELECT
  USING (suite_id = public.current_suite_id());

DROP POLICY IF EXISTS action_receipts_insert ON public.frontdesk_action_receipts;
CREATE POLICY action_receipts_insert ON public.frontdesk_action_receipts FOR INSERT
  WITH CHECK (suite_id = public.current_suite_id());


-- =====================================================================
-- SECTION 3: INVARIANT INDEXES
-- =====================================================================

-- One Aspire duplex business number per owner
CREATE UNIQUE INDEX IF NOT EXISTS ux_business_lines_duplex_per_owner
  ON public.business_lines (suite_id, owner_office_id)
  WHERE (line_mode = 'ASPIRE_FULL_DUPLEX');


-- =====================================================================
-- SECTION 4: SECURITY DEFINER FUNCTIONS
-- =====================================================================

-- Resolve suite by business number (DID) — used by webhook ingress BEFORE JWT claims
CREATE OR REPLACE FUNCTION public.frontdesk_resolve_suite_by_business_number(p_e164 TEXT)
RETURNS TABLE (
  suite_id         UUID,
  business_line_id UUID,
  owner_office_id  UUID,
  line_mode        frontdesk_line_mode,
  business_number  TEXT,
  existing_number  TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT bl.suite_id,
         bl.business_line_id,
         bl.owner_office_id,
         bl.line_mode,
         bl.business_number,
         bl.existing_number
  FROM public.business_lines bl
  WHERE bl.business_number = p_e164
  LIMIT 1;
$$;


-- Outbox claim: atomic claim of pending jobs (worker-safe)
CREATE OR REPLACE FUNCTION public.frontdesk_outbox_claim(p_worker_id TEXT, p_max_jobs INT DEFAULT 20)
RETURNS SETOF public.frontdesk_outbox_jobs
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = public
AS $$
  WITH cte AS (
    SELECT job_id
    FROM public.frontdesk_outbox_jobs
    WHERE status = 'pending'
      AND (run_after IS NULL OR run_after <= now())
    ORDER BY created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT p_max_jobs
  )
  UPDATE public.frontdesk_outbox_jobs j
  SET status = 'running',
      locked_at = now(),
      locked_by = p_worker_id,
      attempts = attempts + 1,
      updated_at = now()
  FROM cte
  WHERE j.job_id = cte.job_id
  RETURNING j.*;
$$;


-- Outbox complete: mark job as succeeded
CREATE OR REPLACE FUNCTION public.frontdesk_outbox_complete(p_job_id UUID)
RETURNS VOID
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = public
AS $$
  UPDATE public.frontdesk_outbox_jobs
  SET status = 'succeeded',
      updated_at = now()
  WHERE job_id = p_job_id;
$$;


-- Outbox fail: mark job as failed with exponential backoff
CREATE OR REPLACE FUNCTION public.frontdesk_outbox_fail(p_job_id UUID, p_error TEXT, p_retry_after_seconds INT DEFAULT 30)
RETURNS VOID
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_attempts INT;
  v_max_attempts INT;
BEGIN
  SELECT attempts, max_attempts INTO v_attempts, v_max_attempts
  FROM public.frontdesk_outbox_jobs
  WHERE job_id = p_job_id;

  IF v_attempts IS NULL THEN
    RETURN;
  END IF;

  IF v_attempts >= v_max_attempts THEN
    UPDATE public.frontdesk_outbox_jobs
    SET status = 'failed',
        last_error = p_error,
        updated_at = now()
    WHERE job_id = p_job_id;
  ELSE
    UPDATE public.frontdesk_outbox_jobs
    SET status = 'pending',
        last_error = p_error,
        run_after = now() + make_interval(secs => p_retry_after_seconds),
        updated_at = now()
    WHERE job_id = p_job_id;
  END IF;
END;
$$;


COMMIT;
