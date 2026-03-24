-- Create system tenant + suite for internal orchestrator receipts
-- The orchestrator uses suite_id="system" which coerces to UUID nil
-- The trigger then resolves nil UUID to the system suite

INSERT INTO public.tenants (tenant_id, name) VALUES ('system', 'Aspire System') ON CONFLICT DO NOTHING;
INSERT INTO app.suites (suite_id, tenant_id, name)
VALUES ('00000000-0000-0000-0000-000000000000'::uuid, 'system', 'System (Internal)')
ON CONFLICT DO NOTHING;

-- Update trigger to handle NULL suite_id gracefully (set to system UUID)
CREATE OR REPLACE FUNCTION trust_sync_tenant_id_from_suite()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
AS $function$
DECLARE
  v_tenant_id text;
  v_resolved_suite_id uuid;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.suite_id IS DISTINCT FROM OLD.suite_id THEN
      RAISE EXCEPTION 'suite_id is immutable';
    END IF;
  END IF;

  IF NEW.suite_id IS NULL THEN
    v_resolved_suite_id := '00000000-0000-0000-0000-000000000000'::uuid;
    NEW.suite_id := v_resolved_suite_id;
  ELSE
    v_resolved_suite_id := NEW.suite_id;
  END IF;

  SELECT tenant_id INTO v_tenant_id FROM app.suites WHERE suite_id = v_resolved_suite_id;
  IF v_tenant_id IS NULL OR btrim(v_tenant_id) = '' THEN
    RAISE EXCEPTION 'unknown suite_id';
  END IF;

  NEW.tenant_id := v_tenant_id;
  RETURN NEW;
END;
$function$;
