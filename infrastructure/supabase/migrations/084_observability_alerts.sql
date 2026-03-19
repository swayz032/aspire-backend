-- Migration 084: Observability Alert Functions + pg_cron Schedules
-- Creates 3 SECURITY DEFINER functions that auto-detect failures and emit alert receipts.
-- pg_cron schedules: failure rate (15min), dead tables (6h), agent heartbeat (15min).

-- 1. Failure Rate Alert — P1 if any receipt_type >50% failure AND >=5 receipts in 1 hour
CREATE OR REPLACE FUNCTION public.check_failure_rate_alerts()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $function$
DECLARE
  rec RECORD;
  alert_exists BOOLEAN;
  v_suite_id uuid;
BEGIN
  SELECT suite_id INTO v_suite_id FROM app.suites LIMIT 1;
  IF v_suite_id IS NULL THEN
    RAISE NOTICE 'No suites found — skipping failure rate alerts';
    RETURN;
  END IF;

  FOR rec IN
    SELECT
      receipt_type,
      COUNT(*) AS total,
      COUNT(*) FILTER (WHERE status IN ('FAILED', 'DENIED')) AS failed,
      ROUND(
        (COUNT(*) FILTER (WHERE status IN ('FAILED', 'DENIED'))::numeric / NULLIF(COUNT(*), 0)) * 100, 1
      ) AS failure_rate
    FROM public.receipts
    WHERE created_at > NOW() - INTERVAL '1 hour'
      AND receipt_type IS NOT NULL
      AND receipt_type != ''
    GROUP BY receipt_type
    HAVING COUNT(*) >= 5
      AND (COUNT(*) FILTER (WHERE status IN ('FAILED', 'DENIED'))::numeric / NULLIF(COUNT(*), 0)) > 0.5
  LOOP
    SELECT EXISTS(
      SELECT 1 FROM public.receipts
      WHERE receipt_type = 'alert.failure_rate'
        AND created_at > NOW() - INTERVAL '1 hour'
        AND result->>'alerted_receipt_type' = rec.receipt_type
    ) INTO alert_exists;

    IF NOT alert_exists THEN
      INSERT INTO public.receipts (
        receipt_id, suite_id, office_id, receipt_type, status,
        correlation_id, actor_type, actor_id, action, result, created_at
      ) VALUES (
        gen_random_uuid()::text,
        v_suite_id,
        v_suite_id,
        'alert.failure_rate',
        'FAILED',
        'alert-' || gen_random_uuid()::text,
        'SYSTEM',
        'observability_monitor',
        jsonb_build_object(
          'type', 'failure_rate_alert',
          'severity', 'P1',
          'threshold', 50,
          'window', '1 hour'
        ),
        jsonb_build_object(
          'alerted_receipt_type', rec.receipt_type,
          'failure_rate', rec.failure_rate,
          'total_count', rec.total,
          'failed_count', rec.failed,
          'message', rec.receipt_type || ' has ' || rec.failure_rate || '% failure rate (' || rec.failed || '/' || rec.total || ' in last hour)'
        ),
        NOW()
      );
    END IF;
  END LOOP;
END;
$function$;

-- 2. Dead Table Alert — P2 if provider_call_log or client_events have 0 rows in 24h
CREATE OR REPLACE FUNCTION public.check_dead_table_alerts()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $function$
DECLARE
  tbl TEXT;
  row_count BIGINT;
  alert_exists BOOLEAN;
  v_suite_id uuid;
  tables_to_check TEXT[] := ARRAY['provider_call_log', 'client_events'];
BEGIN
  SELECT suite_id INTO v_suite_id FROM app.suites LIMIT 1;
  IF v_suite_id IS NULL THEN
    RAISE NOTICE 'No suites found — skipping dead table alerts';
    RETURN;
  END IF;

  FOREACH tbl IN ARRAY tables_to_check
  LOOP
    EXECUTE format(
      'SELECT COUNT(*) FROM public.%I WHERE created_at > NOW() - INTERVAL ''24 hours''',
      tbl
    ) INTO row_count;

    IF row_count = 0 THEN
      SELECT EXISTS(
        SELECT 1 FROM public.receipts
        WHERE receipt_type = 'alert.dead_table'
          AND created_at > NOW() - INTERVAL '6 hours'
          AND result->>'table_name' = tbl
      ) INTO alert_exists;

      IF NOT alert_exists THEN
        INSERT INTO public.receipts (
          receipt_id, suite_id, office_id, receipt_type, status,
          correlation_id, actor_type, actor_id, action, result, created_at
        ) VALUES (
          gen_random_uuid()::text,
          v_suite_id,
          v_suite_id,
          'alert.dead_table',
          'FAILED',
          'alert-' || gen_random_uuid()::text,
          'SYSTEM',
          'observability_monitor',
          jsonb_build_object(
            'type', 'dead_table_alert',
            'severity', 'P2',
            'window', '24 hours'
          ),
          jsonb_build_object(
            'table_name', tbl,
            'row_count_24h', 0,
            'message', tbl || ' has 0 rows in the last 24 hours — observability pipeline may be disconnected'
          ),
          NOW()
        );
      END IF;
    END IF;
  END LOOP;
END;
$function$;

-- 3. Agent Heartbeat Alert — P2 if n8n agents silent >30 min
CREATE OR REPLACE FUNCTION public.check_agent_heartbeat_alerts()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $function$
DECLARE
  rec RECORD;
  alert_exists BOOLEAN;
  v_suite_id uuid;
BEGIN
  SELECT suite_id INTO v_suite_id FROM app.suites LIMIT 1;
  IF v_suite_id IS NULL THEN
    RAISE NOTICE 'No suites found — skipping heartbeat alerts';
    RETURN;
  END IF;

  FOR rec IN
    SELECT
      split_part(receipt_type, '.', 1) AS agent_prefix,
      MAX(created_at) AS last_seen,
      EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) / 60 AS minutes_silent
    FROM public.receipts
    WHERE created_at > NOW() - INTERVAL '7 days'
      AND receipt_type IS NOT NULL
      AND receipt_type != ''
      AND receipt_type LIKE 'n8n_%'
    GROUP BY split_part(receipt_type, '.', 1)
    HAVING EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) / 60 > 30
  LOOP
    SELECT EXISTS(
      SELECT 1 FROM public.receipts
      WHERE receipt_type = 'alert.agent_heartbeat'
        AND created_at > NOW() - INTERVAL '30 minutes'
        AND result->>'agent_prefix' = rec.agent_prefix
    ) INTO alert_exists;

    IF NOT alert_exists THEN
      INSERT INTO public.receipts (
        receipt_id, suite_id, office_id, receipt_type, status,
        correlation_id, actor_type, actor_id, action, result, created_at
      ) VALUES (
        gen_random_uuid()::text,
        v_suite_id,
        v_suite_id,
        'alert.agent_heartbeat',
        'FAILED',
        'alert-' || gen_random_uuid()::text,
        'SYSTEM',
        'observability_monitor',
        jsonb_build_object(
          'type', 'agent_heartbeat_alert',
          'severity', 'P2',
          'threshold_minutes', 30
        ),
        jsonb_build_object(
          'agent_prefix', rec.agent_prefix,
          'last_seen', rec.last_seen,
          'minutes_silent', ROUND(rec.minutes_silent::numeric, 1),
          'message', rec.agent_prefix || ' has been silent for ' || ROUND(rec.minutes_silent::numeric, 0) || ' minutes (last seen: ' || rec.last_seen || ')'
        ),
        NOW()
      );
    END IF;
  END LOOP;
END;
$function$;

-- pg_cron schedules (idempotent — cron.schedule replaces if name exists)
SELECT cron.schedule('check-failure-rate-alerts', '*/15 * * * *', 'SELECT public.check_failure_rate_alerts()');
SELECT cron.schedule('check-dead-table-alerts', '0 */6 * * *', 'SELECT public.check_dead_table_alerts()');
SELECT cron.schedule('check-agent-heartbeat-alerts', '*/15 * * * *', 'SELECT public.check_agent_heartbeat_alerts()');
