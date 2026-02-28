# ID Crosswalk: Patent Docs ↔ Aspire Implementation

## Canonical Aspire identities
- `suite_id` = **Company** identity in Aspire (tenant boundary).
- `office_number` = **Seat label** inside a company (display identity: member/team office number).
- `office_id` = **Seat identity** (UUID PK recommended). `office_number` should be unique per `suite_id` but not the PK.

## Crosswalk from patent pack terminology
Patent pack often uses:
- `tenant_id` → map to `suite_id`
- `user_id` / `actor_id` → map to `office_id` (and optionally also `user_id` if you keep both)
- `risk_tier` uses `GREEN/YELLOW/RED` in some docs:
  - `GREEN` → `low`
  - `YELLOW` → `medium`
  - `RED` → `high`

## Guidance for Trust Spine data model
Wherever a high-risk decision is recorded, include:
- `suite_id` (required)
- `office_id` (recommended; who did it)
- `actor_type` (`human|agent|system`)
- `actor_id`
- trace context (`trace_id`, `run_id`, `span_id`)

## Where to enforce this
- DB tables: approvals, receipts, outbox jobs, provider call logs, A2A/authority items
- API: gateway endpoints should derive `suite_id` from auth context and validate `office_id` membership
