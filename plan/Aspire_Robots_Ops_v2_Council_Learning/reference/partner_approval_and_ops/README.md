# Aspire Partner Approval + Ops Handoff v2
Date: 2026-01-30

This pack covers **non-Trust-Spine** workstreams that materially impact production readiness and partner approvals
(e.g., Gusto, Plaid Transfer) and provides Claude-ready scaffolding and templates.

## How to use
Recommended placement inside your repo:
- `/docs/approval-packets/*` (reviewer-ready bundles)
- `/docs/program/*` (program design + SOPs)
- `/docs/security/*` (security posture + questionnaires)
- `/docs/ops/*` (support + on-call + SLAs)
- `/docs/evidence/*` (export scripts + replay bundles)
- `/gateway/webhooks/*` (signature verification + ingestion patterns)

## What changed from v1
- Added reviewer-facing **approval packets** (checklists + exact attachments to include).
- Added an **evidence pack generator** script to export receipts/provider logs and assemble a replay bundle.
- Added templates for security questionnaires, SLAs, and status page comms.

## What Claude should implement first
See `CLAUDE_HANDOFF_ADDON/01_WORKLIST.md`.
