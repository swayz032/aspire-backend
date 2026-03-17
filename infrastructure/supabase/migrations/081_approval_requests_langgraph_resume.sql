-- Migration 081: Persist LangGraph resume metadata on approval_requests
-- Enables native LangGraph interrupt/resume while preserving the existing
-- approval_requests table as the audit/operator source of truth.

ALTER TABLE public.approval_requests
  ADD COLUMN IF NOT EXISTS request_id TEXT,
  ADD COLUMN IF NOT EXISTS thread_id TEXT,
  ADD COLUMN IF NOT EXISTS session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_approval_requests_thread_id
  ON public.approval_requests (thread_id)
  WHERE thread_id IS NOT NULL;
