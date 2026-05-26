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

## Blueprint Pipeline Receipt Mapping (Track B Pass 1.C)
- `blueprint.ingest.azure_doc_intel_used` -> emitted in `ocr_coordinator.py` (OCR path only, NOT all sheets — scanned/image-heavy pages only call Azure; text-layer pages use LlamaParse)
- `provider.azure.sheet_layout` -> emitted in `universal_sheet_extractor.py` (SEPARATE receipt type, different pipeline: universal_sheet_intel for blueprint_sheet_intelligence)
- `drew.see.legend_extracted` -> emitted in `vgse_pipeline.py`, tracks legend_pairs_extracted + legend_memory_upserts (writes to blueprint_project_legends)
- `drew.see.11g2.page` -> emitted in `wave_11g2_see.py`, tracks n_instance_rows inserted into `blueprint_symbols` (NOT blueprint_primitives — plan doc has wrong table name)
- `drew.see.11g2.completed` -> emitted in `wave_11g2_see.py`, tracks rows_inserted to `blueprint_symbols` (plan doc incorrectly says blueprint_composed_symbols + blueprint_primitives)
- `blueprint.see.completed` -> emitted in `drew_blueprint.py`, tracks symbol_count in `blueprint_symbols` (plan doc incorrectly says blueprint_primitives)
- `drew.see.spatial_binding` -> emitted in `vgse_pipeline.py`, advisory — writes to symbol_memory (blueprint_symbol_memory table), correctly expected_min=0
- wave_11g2 is OFF by default (ASPIRE_USE_WAVE_11G2 env var, default false) — Gulfstream's 1.28M primitives came via VGSE, not 11g2
- VGSE writes to `blueprint_symbols` via drew_blueprint.py; distinct from 11g2 path
- Gulfstream has 8 motifs in blueprint_composed_symbols — 0 rows from `drew.see.11g2.page` would be EXPECTED (feature flag OFF)
