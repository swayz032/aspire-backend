# Aspire Test Engineer Memory

## Test Infrastructure
- Framework: pytest with WSL2 (Ubuntu-22.04)
- Run command: `wsl -d Ubuntu-22.04 -e bash -c "cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator && source ~/venvs/aspire/bin/activate && python -m pytest tests/ -q --tb=short"`
- Test baseline (v1.1.1): 2802 passed, 567 failed (pre-existing: persona maps, _policies attr)
- Coverage target: ≥80%

## C3 Rotation Scan Findings (2026-03-23)
See `c3-rotation-bugs.md` for full bug list. Key findings:
- BUG-01 BLOCKER: `supabase_url=""` and `supabase_service_role_key=""` defaults — no startup crash in dev
- BUG-02 HIGH: `s2s_hmac_secret=""` default — Domain Rail S2S auth silently disabled
- BUG-03 HIGH: `quickbooks_base_url=""` default (comment says sandbox, but empty string = runtime failure)
- BUG-04 HIGH: Stripe webhook singleton initialized with `""` secret — not fail-closed
- BUG-05 HIGH: `allow_internal_routing` uses header presence check only, not JWT validity
- BUG-06 MEDIUM: PandaDoc webhook receipt never persisted to store — only logged
- BUG-07 MEDIUM: Twilio webhook receipt never persisted to store — only logged
- BUG-08 MEDIUM: `_require_admin` lazy-imports `jwt` inside function — ImportError not handled
- BUG-09 MEDIUM: Exception handler receipt missing `trace_id` field (required field gap)
- BUG-10 LOW: `_verify_environment_parity()` called at module import time, before secrets load
- BUG-11 LOW: `store_receipts_strict` accesses `_receipt_writer._loop` (private attr) — fragile
- BUG-12 LOW: Approval binding check 7 (request_id match) comes AFTER the key is marked used

## Key Architecture Notes
- Admin auth: X-Admin-Token (HS256 JWT with ASPIRE_ADMIN_JWT_SECRET) — no dev bypass
- Tenant isolation: enforced in receipt_store.query_receipts via suite_id filter
- Webhook auth: provider-specific (Stripe SDK, PandaDoc HMAC-SHA256, Twilio RequestValidator)
- Token TTL: 45s default, max 59s (Law #5 compliant)
- Rate limit: 500/60s default, per-tenant (suite_id), Redis-backed in prod
- Receipts: dual-write (in-memory always + async Supabase when configured)
- Middleware order: ChaosMonkey → GlobalException → RateLimit → CorrelationId → CORS (outermost)
