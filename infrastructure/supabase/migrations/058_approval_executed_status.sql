-- Migration 058: Allow 'executed' status for approval_requests
-- Phase 3 W14: Resume pipeline needs approved → executed transition
--
-- The approval lifecycle is:
--   pending → approved → executed  (happy path)
--   pending → rejected/expired/canceled  (terminal)
--
-- Previously 'approved' was terminal. Now 'approved' → 'executed' is valid
-- because the resume node marks drafts as executed after tool execution.

-- Step 1: Drop and recreate the CHECK constraint to include 'executed'
ALTER TABLE approval_requests
  DROP CONSTRAINT IF EXISTS approval_requests_status_check;

ALTER TABLE approval_requests
  ADD CONSTRAINT approval_requests_status_check
  CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'canceled', 'executed'));

-- Step 2: Update the state transition trigger to allow approved → executed
CREATE OR REPLACE FUNCTION public.enforce_approval_state_transitions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF (tg_op = 'UPDATE') THEN
    -- Terminal statuses: rejected, expired, canceled, executed (NOT approved — it can → executed)
    IF old.status IN ('rejected', 'expired', 'canceled', 'executed') AND new.status <> old.status THEN
      RAISE EXCEPTION 'cannot transition from terminal status %', old.status;
    END IF;

    -- From approved: only 'executed' is valid
    IF old.status = 'approved' AND new.status NOT IN ('approved', 'executed') THEN
      RAISE EXCEPTION 'invalid approval status transition from approved to %', new.status;
    END IF;

    -- From pending: approved, rejected, expired, canceled are valid
    IF old.status = 'pending' THEN
      IF new.status NOT IN ('pending', 'approved', 'rejected', 'expired', 'canceled') THEN
        RAISE EXCEPTION 'invalid approval status transition';
      END IF;
    END IF;

    IF new.status = 'approved' THEN
      IF new.decided_at IS NULL OR new.decided_by_user_id IS NULL THEN
        RAISE EXCEPTION 'approved requires decided_at and decided_by_user_id';
      END IF;
    END IF;
  END IF;

  RETURN new;
END;
$$;
