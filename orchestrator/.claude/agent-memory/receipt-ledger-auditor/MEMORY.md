# Receipt Ledger Auditor — Agent Memory

## Key Files
- [audit-patterns.md](audit-patterns.md) — Receipt schema patterns, known gaps, recurring issues
- [cycle-history.md](cycle-history.md) — Per-cycle audit findings summary

## Quick Reference
- Backend routes: `src/aspire_orchestrator/routes/` (admin.py, intents.py, robots.py, webhooks.py)
- Server.py: houses /v1/intents, /v1/a2a/*, /v1/resume/{id} endpoints
- Desktop gateway: `Aspire-desktop/server/routes.ts`
- Known systemic debt (do NOT re-report): receipt_hash="" providers, 42% coverage (55/130), 9 rule-based skillpacks
- PandaDoc/Twilio webhook receipts are built but NEVER written to DB (only logger.info)
- ingest_client_event (POST /admin/ops/client-events) has NO receipt emission — silent state change
- POST /api/auth/signup and POST /api/auth/validate-invite-code have NO receipt emission
- admin.ops.triage and admin.ops.provider-analysis endpoints have NO receipt emission for success path
- Bootstrap outer catch (routes.ts:963-976) logs failure but never writes the failure receipt to DB
- booking.cancel receipt emission is fire-and-forget (warn only, not fail-closed) — RED risk
- DELETE /api/services/:serviceId receipt is warn-only on failure, not fail-closed
