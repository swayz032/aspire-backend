-- =============================================================================
-- Migration 100: Backfill agent_episodes + agent_semantic_memory -> memory_objects
-- =============================================================================
-- Pass 7 Lane A of the Office Memory Engine plan.
--
-- Goal: Mirror existing per-agent memory tables into the new memory_objects
--       spine in shadow mode. The legacy tables stay live; cutover happens
--       in Pass 12.
--
-- Aspire Laws enforced:
--   Law #2 (Receipts)    -- INSERT goes through the standard memory_objects
--                           pathway; idempotency guarded by uq_memory_objects_idempotency.
--                           No bypass of the BEFORE INSERT/UPDATE/DELETE triggers.
--   Law #3 (Fail Closed) -- Rows missing required columns are skipped
--                           (NULL idempotency_key, NULL summary).
--   Law #6 (Tenant Isolation) -- tenant_id := suite_id (canonical convention,
--                           confirmed by nodes/resume.py:89 and the dominant
--                           pattern in receipts where 27,743 of 29,583 rows have
--                           office_id = suite_id). Each backfilled row carries the
--                           same suite_id it had in the legacy table.
--   Law #9 (Security)    -- Migration runs as service_role; PII (summary text,
--                           fact_value) is preserved as-is from source rows
--                           (no new PII is created or logged).
--
-- Idempotency:
--   - idempotency_key = 'episode:'||ae.id  (for agent_episodes)
--   - idempotency_key = 'semantic:'||asm.id  (for agent_semantic_memory)
--   - Re-running this migration adds zero rows because of the WHERE NOT EXISTS
--     pre-check on (tenant_id, suite_id, idempotency_key). Note: the matching
--     uq_memory_objects_idempotency unique constraint is deferrable, so
--     ON CONFLICT cannot use it as an arbiter -- pre-check is the sole guard.
--
-- source_agent enum coercion:
--   memory_objects.source_agent CHECK accepts only:
--     'ava','sarah','eli','nora','finn','tim','system'
--   We apply CASE LOWER(agent_id) ... ELSE 'system' so any unknown text
--   (e.g. 'sarah-front-desk', 'sarah-receptionist', 'finn-finance')
--   collapses to the canonical value or to 'system'.
--
-- channel enum coercion:
--   memory_objects.channel CHECK accepts:
--     'voice','video','email','sms','workflow','finance','ui','webhook'
--   agent_episodes don't carry channel info, so we default to 'voice' for
--   episodes (most session_summary rows originate from Ava/Sarah voice flows)
--   and 'workflow' for agent_semantic_memory (facts come from offline analysis).
--
-- runtime_family default:
--   'internal' for both backfill paths -- legacy rows predate the
--   ElevenLabs/Anam runtime split, so 'internal' is the safest provenance label.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Backfill agent_episodes -> memory_objects (memory_type='session_summary')
-- -----------------------------------------------------------------------------
INSERT INTO public.memory_objects (
    memory_id,
    tenant_id, suite_id, office_id,
    memory_type, schema_version,
    source_surface, source_agent, runtime_family, channel,
    trace_id, correlation_id,
    title, summary, detail,
    visibility_scope, status,
    embedding,
    event_at, created_at, last_activity_at,
    idempotency_key
)
SELECT
    ae.id                                    AS memory_id,
    ae.suite_id                              AS tenant_id,   -- canonical: tenant_id := suite_id
    ae.suite_id                              AS suite_id,
    ae.suite_id                              AS office_id,   -- canonical: office_id := suite_id (no separate office layer in V0)
    'session_summary'                        AS memory_type,
    'v1'                                     AS schema_version,
    NULL                                     AS source_surface,
    CASE LOWER(COALESCE(ae.agent_id, ''))
        WHEN 'ava'                  THEN 'ava'
        WHEN 'finn'                 THEN 'finn'
        WHEN 'eli'                  THEN 'eli'
        WHEN 'nora'                 THEN 'nora'
        WHEN 'tim'                  THEN 'tim'
        WHEN 'sarah'                THEN 'sarah'
        WHEN 'sarah-front-desk'     THEN 'sarah'
        WHEN 'sarah-frontdesk'      THEN 'sarah'
        WHEN 'sarah-receptionist'   THEN 'sarah'
        WHEN 'finn-finance'         THEN 'finn'
        ELSE 'system'
    END                                      AS source_agent,
    'internal'                               AS runtime_family,
    'voice'                                  AS channel,
    ae.id                                    AS trace_id,        -- deterministic re-use of the legacy id
    ae.id                                    AS correlation_id,  -- deterministic re-use of the legacy id
    NULL                                     AS title,
    COALESCE(NULLIF(TRIM(ae.summary), ''),
             '[migrated session_summary with empty body]')
                                             AS summary,
    jsonb_build_object(
        'key_topics',        COALESCE(to_jsonb(ae.key_topics), '[]'::jsonb),
        'key_entities',      COALESCE(ae.key_entities, '{}'::jsonb),
        'turn_count',        COALESCE(ae.turn_count, 0),
        'session_id',        ae.session_id,
        'legacy_user_id',    ae.user_id,
        'legacy_agent_id',   ae.agent_id,
        'migration_source',  'agent_episodes',
        'migration_version', '100'
    )                                        AS detail,
    'office'                                 AS visibility_scope,
    'promoted'                               AS status,         -- backfilled rows are post-hoc, not requested/drafted
    ae.embedding                             AS embedding,      -- vector(1536) shape preserved
    ae.created_at                            AS event_at,
    ae.created_at                            AS created_at,
    ae.created_at                            AS last_activity_at,
    'episode:' || ae.id::text                AS idempotency_key
FROM public.agent_episodes ae
WHERE ae.suite_id IS NOT NULL
  AND ae.id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.memory_objects mo
      WHERE mo.tenant_id        = ae.suite_id
        AND mo.suite_id         = ae.suite_id
        AND mo.idempotency_key  = 'episode:' || ae.id::text
  );

-- -----------------------------------------------------------------------------
-- 2. Backfill agent_semantic_memory -> memory_objects (memory_type='decision_fact')
-- -----------------------------------------------------------------------------
INSERT INTO public.memory_objects (
    memory_id,
    tenant_id, suite_id, office_id,
    memory_type, schema_version,
    source_surface, source_agent, runtime_family, channel,
    trace_id, correlation_id,
    title, summary, detail, confidence,
    visibility_scope, status,
    event_at, created_at, last_activity_at,
    idempotency_key
)
SELECT
    asm.id                                   AS memory_id,
    asm.suite_id                             AS tenant_id,
    asm.suite_id                             AS suite_id,
    asm.suite_id                             AS office_id,
    'decision_fact'                          AS memory_type,
    'v1'                                     AS schema_version,
    NULL                                     AS source_surface,
    CASE LOWER(COALESCE(asm.agent_id, ''))
        WHEN 'ava'                  THEN 'ava'
        WHEN 'finn'                 THEN 'finn'
        WHEN 'eli'                  THEN 'eli'
        WHEN 'nora'                 THEN 'nora'
        WHEN 'tim'                  THEN 'tim'
        WHEN 'sarah'                THEN 'sarah'
        WHEN 'sarah-front-desk'     THEN 'sarah'
        WHEN 'sarah-frontdesk'      THEN 'sarah'
        WHEN 'sarah-receptionist'   THEN 'sarah'
        WHEN 'finn-finance'         THEN 'finn'
        ELSE 'system'
    END                                      AS source_agent,
    'internal'                               AS runtime_family,
    'workflow'                               AS channel,
    asm.id                                   AS trace_id,
    asm.id                                   AS correlation_id,
    asm.fact_key                             AS title,
    COALESCE(NULLIF(TRIM(asm.fact_value), ''),
             '[migrated decision_fact with empty value]')
                                             AS summary,
    jsonb_build_object(
        'fact_type',          asm.fact_type,
        'fact_key',           asm.fact_key,
        'fact_value',         asm.fact_value,
        'legacy_user_id',     asm.user_id,
        'legacy_agent_id',    asm.agent_id,
        'source_episode_id',  asm.source_episode_id,
        'migration_source',   'agent_semantic_memory',
        'migration_version',  '100'
    )                                        AS detail,
    GREATEST(0.0::float8, LEAST(1.0::float8, COALESCE(asm.confidence, 0.5)))
                                             AS confidence,     -- clamp to [0,1]
    'office'                                 AS visibility_scope,
    'promoted'                               AS status,
    COALESCE(asm.created_at, asm.updated_at, NOW())             AS event_at,
    COALESCE(asm.created_at, asm.updated_at, NOW())             AS created_at,
    COALESCE(asm.updated_at, asm.created_at, NOW())             AS last_activity_at,
    'semantic:' || asm.id::text              AS idempotency_key
FROM public.agent_semantic_memory asm
WHERE asm.suite_id IS NOT NULL
  AND asm.id IS NOT NULL
  AND COALESCE(asm.confidence, 0.5) > 0.0      -- skip soft-deleted facts (forget() sets confidence=0)
  AND NOT EXISTS (
      SELECT 1 FROM public.memory_objects mo
      WHERE mo.tenant_id        = asm.suite_id
        AND mo.suite_id         = asm.suite_id
        AND mo.idempotency_key  = 'semantic:' || asm.id::text
  );

-- -----------------------------------------------------------------------------
-- 3. Verification block (RAISE NOTICE shows source vs destination row counts)
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    src_episodes        BIGINT;
    src_episodes_valid  BIGINT;
    dst_from_episodes   BIGINT;
    src_semantic        BIGINT;
    src_semantic_valid  BIGINT;
    dst_from_semantic   BIGINT;
BEGIN
    SELECT count(*) INTO src_episodes FROM public.agent_episodes;
    SELECT count(*) INTO src_episodes_valid FROM public.agent_episodes WHERE suite_id IS NOT NULL AND id IS NOT NULL;
    SELECT count(*) INTO dst_from_episodes  FROM public.memory_objects WHERE detail->>'migration_source' = 'agent_episodes';

    SELECT count(*) INTO src_semantic FROM public.agent_semantic_memory;
    SELECT count(*) INTO src_semantic_valid FROM public.agent_semantic_memory
        WHERE suite_id IS NOT NULL AND id IS NOT NULL AND COALESCE(confidence, 0.5) > 0.0;
    SELECT count(*) INTO dst_from_semantic  FROM public.memory_objects WHERE detail->>'migration_source' = 'agent_semantic_memory';

    RAISE NOTICE '[mig 100] agent_episodes: src=%, src_valid=%, dst=%, delta=%',
        src_episodes, src_episodes_valid, dst_from_episodes, src_episodes_valid - dst_from_episodes;
    RAISE NOTICE '[mig 100] agent_semantic_memory: src=%, src_valid=%, dst=%, delta=%',
        src_semantic, src_semantic_valid, dst_from_semantic, src_semantic_valid - dst_from_semantic;

    IF src_episodes_valid <> dst_from_episodes THEN
        RAISE WARNING '[mig 100] episode backfill drift: % source rows valid, % destination rows -- investigate before Pass 12 cutover',
            src_episodes_valid, dst_from_episodes;
    END IF;
    IF src_semantic_valid <> dst_from_semantic THEN
        RAISE WARNING '[mig 100] semantic backfill drift: % source rows valid, % destination rows -- investigate before Pass 12 cutover',
            src_semantic_valid, dst_from_semantic;
    END IF;
END $$;

COMMIT;
