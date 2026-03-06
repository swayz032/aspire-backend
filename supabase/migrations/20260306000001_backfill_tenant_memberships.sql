-- Backfill tenant_memberships for existing suite owners
-- Root cause: bootstrap endpoint created app.suites but never inserted tenant_memberships.
-- Without this row, app.check_suite_access() Path A (auth.uid() check) fails,
-- blocking ALL client-side RLS-scoped queries.
--
-- Two strategies:
--   1. Match via user_metadata->>'suite_id' (users where bootstrap completed step 6)
--   2. Match via deterministic tenant_id pattern (users where bootstrap failed before step 6)
-- ON CONFLICT DO NOTHING ensures idempotency (safe to re-run).

-- Strategy 1: Users who have suite_id in their metadata
INSERT INTO tenant_memberships (tenant_id, user_id, role)
SELECT s.tenant_id, u.id, 'owner'
FROM app.suites s
JOIN auth.users u ON u.raw_user_meta_data->>'suite_id' = s.suite_id::text
WHERE NOT EXISTS (
  SELECT 1 FROM tenant_memberships m
  WHERE m.tenant_id = s.tenant_id AND m.user_id = u.id
)
ON CONFLICT (tenant_id, user_id) DO NOTHING;

-- Strategy 2: Match via deterministic tenant_id = 'tenant-' || left(replace(user_id, '-', ''), 16)
-- Catches users whose bootstrap created the suite but failed before writing user_metadata
INSERT INTO tenant_memberships (tenant_id, user_id, role)
SELECT s.tenant_id, u.id, 'owner'
FROM auth.users u
JOIN app.suites s ON s.tenant_id = 'tenant-' || left(replace(u.id::text, '-', ''), 16)
WHERE NOT EXISTS (
  SELECT 1 FROM tenant_memberships m
  WHERE m.tenant_id = s.tenant_id AND m.user_id = u.id
)
ON CONFLICT (tenant_id, user_id) DO NOTHING;
