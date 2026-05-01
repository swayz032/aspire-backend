---
name: Round 7 Wave A — Adam multi-store + briefing enrichment
description: Files edited and patterns introduced for Adam production hardening + Ava briefing enrichment in Round 7 Wave A
type: project
---

# Round 7 Wave A summary (2026-04-30)

Plan: `C:\Users\tonio\.claude\plans\hey-can-you-deep-serene-elephant.md`

## Files edited

- `Aspire-desktop/server/agentToolRoutes.ts`
  - `/v1/tools/context` (~L450-560): expanded suite_profiles SELECT to whitelist of onboarding fields; derived `first_name`, `salutation`, `gender_pronoun`; returned `office_city/state`, `home_city/state`, `years_in_business`, `currency`, etc. Privacy whitelist enforced — no street address or DOB.
  - Invoke proxy (~L1190): forwards `include_other_stores` (boolean) from desktop request body to orchestrator when present.

- `Aspire-desktop/scripts/sync-anam-ava-canonical.mjs`
  - Added `include_other_stores` boolean field to `invoke_adam` Anam tool schema with strict opt-in description.

- `backend/orchestrator/src/aspire_orchestrator/services/adam/schemas/playbook_context.py`
  - Added `include_other_stores: bool = False` to `PlaybookContext` dataclass.

- `backend/orchestrator/src/aspire_orchestrator/server.py`
  - extra_kwargs list (~L1830) now includes `("include_other_stores", "include_other_stores")` for `TOOL_MATERIAL_PRICE_CHECK`.

- `backend/orchestrator/src/aspire_orchestrator/services/adam/playbooks/trades.py`
  - New module constants: `HD_TOO_FAR_MILES = 25.0`, `_SHOPPING_RETRY_MAX_ATTEMPTS = 2`, `_SHOPPING_RETRY_BASE_MS = (250, 500)`.
  - New helper `_emit_playbook_receipt()` — playbook-rollup receipt with `actor_type=WORKER`, `action_type=adam.playbook.<name>`, status mapped via outcome lower(), best-effort persistence (logged-and-swallowed on failure).
  - `execute_tool_material_price_check()` signature: added `include_other_stores: bool = False`.
  - Voice-path gate: `run_shopping = (not voice_path) or include_other_stores`. `skip_google_shopping = not run_shopping`.
  - SerpApi shopping wrapper `_shopping_with_backoff()` — exponential backoff with jitter on 429/RATE_LIMITED. Max 2 retries (3 total), 250ms/500ms base + 0-100ms jitter.
  - HD-only filter at the `display_products` step: skipped when `include_other_stores=True` so non-HD records survive into carousel.
  - Decision flags computed for EVERY response (success and failure): `nearest_store_distance_miles`, `hd_too_far`, `hd_has_stock`. Surfaced in `extra` of `ResearchResponse`.
  - Receipts emitted at every outcome:
    - `SUCCEEDED reason=success` — HD-only success
    - `SUCCEEDED reason=multi_store_success` — include_other_stores=true success
    - `SUCCEEDED reason=store_disambiguation` — multi-store-in-city ambiguity
    - `FAILED reason=shopping_429` — SerpApi rate-limited short-circuit
    - `FAILED reason=hd_too_far` — distance > 25mi or no HD within 50km
    - `FAILED reason=no_stock` — HD products found, all out of stock
    - `FAILED reason=missing_required_fields` — generic completeness failure

## Guardrails respected

- No DB migration. Reuses suite_profiles columns already present (per migration 055).
- Tenant isolation preserved — Supabase service-role client filters by suite_id on every SELECT (RLS-equivalent at app layer).
- No PII in logs. `_redact_user_address()` and `_redact_address()` already in place; new code adds nothing raw.
- HD-default behavior unchanged when `include_other_stores=false` — voice path stays HD-only; shopping retry budget only spent when shopping is actually called.

## Receipt coverage

100% of `TOOL_MATERIAL_PRICE_CHECK` outcomes now emit a playbook-rollup receipt. Provider clients (`SerpApiHomeDepotClient`, `SerpApiShoppingClient`, `find_nearest_home_depot_by_address`) continue to emit per-call provider receipts. The new playbook receipt is the agent-level rollup with `actor_type=WORKER` and `action_type=adam.playbook.TOOL_MATERIAL_PRICE_CHECK` — distinguishes Adam's decision from raw provider hits.

## Blockers

None. No tests written or run in this wave (Wave D — `tests-r7` agent).

## Out of scope (handed off)

- Wave B (Anam SDK videoQuality + diagnostic log) — `expo-ava-r7`
- Wave C (prompt edits) — `prompt-ava-r7`
- Wave D (tests) — `tests-r7`
- Wave E verification + Wave 6 push — downstream agents
