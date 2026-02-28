-- Calendar Events table (Phase 3 W2: Calendar system)
-- Stores native calendar events alongside bookings for unified scheduling
-- Law #6: RLS tenant isolation on suite_id
-- Law #2: State changes generate receipts via API layer

CREATE TABLE IF NOT EXISTS calendar_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id UUID NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  event_type TEXT NOT NULL DEFAULT 'meeting'
    CHECK (event_type IN ('meeting','task','reminder','call','deadline','other')),
  start_time TIMESTAMPTZ NOT NULL,
  end_time TIMESTAMPTZ,
  duration_minutes INTEGER,
  location TEXT,
  participants TEXT[],
  is_all_day BOOLEAN DEFAULT false,
  source TEXT DEFAULT 'manual'
    CHECK (source IN ('manual','ava','booking','google_calendar','import')),
  source_ref TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','in_progress','completed','cancelled')),
  completed_at TIMESTAMPTZ,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_cal_events_suite_time ON calendar_events(suite_id, start_time);
CREATE INDEX idx_cal_events_status ON calendar_events(suite_id, status);

ALTER TABLE calendar_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_events FORCE ROW LEVEL SECURITY;

CREATE POLICY cal_events_select ON calendar_events FOR SELECT TO authenticated
  USING (suite_id = (current_setting('app.current_suite_id', true))::uuid);
CREATE POLICY cal_events_insert ON calendar_events FOR INSERT TO authenticated
  WITH CHECK (suite_id = (current_setting('app.current_suite_id', true))::uuid);
CREATE POLICY cal_events_update ON calendar_events FOR UPDATE TO authenticated
  USING (suite_id = (current_setting('app.current_suite_id', true))::uuid)
  WITH CHECK (suite_id = (current_setting('app.current_suite_id', true))::uuid);
CREATE POLICY cal_events_delete ON calendar_events FOR DELETE TO authenticated
  USING (suite_id = (current_setting('app.current_suite_id', true))::uuid);
CREATE POLICY cal_events_service_role ON calendar_events FOR ALL TO service_role
  USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_events TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_events TO service_role;
