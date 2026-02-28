-- Migration 054: approval_requests table
-- NOTE: This table already exists in Supabase with the orchestrator schema
-- (approval_id, tenant_id, tool, operation, risk_tier, etc.)
-- Created by the backend orchestrator's approval_service.py
-- Desktop authority-queue endpoints have been aligned to use the existing schema.
--
-- This migration is intentionally a no-op to avoid conflicts.
-- The orchestrator owns the schema; Desktop reads/writes to it.

-- Verify table exists (will succeed silently if already present)
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'approval_requests') THEN
    RAISE EXCEPTION 'approval_requests table does not exist — run orchestrator migrations first';
  END IF;
END $$;
