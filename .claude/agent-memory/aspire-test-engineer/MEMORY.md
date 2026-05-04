# Aspire Test Engineer Memory

## Test Infrastructure
- Test runner: pytest (WSL2, Ubuntu-22.04, venv ~/venvs/aspire)
- Run command: `wsl -d Ubuntu-22.04 -e bash -c "cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator && source ~/venvs/aspire/bin/activate && python -m pytest tests/ -q --tb=short"`
- Test baseline (last known): 2802 passed, 567 failed (pre-existing plan-scope failures: persona maps, _policies attr)

## Cycle 5 Findings (2026-03-22)

### CRITICAL Bugs Found
1. **`execute.py` line 266** — `_deny_execution()` called BEFORE its `def` at line 312
   - Triggers: `assigned_agent == "eli"` and `task_type in ("email.draft", "email.send")` and params not a dict
   - Effect: `NameError` at runtime, full execution path crashes
   - Fix: Move `def _deny_execution(...)` to before line 263 (before the Eli gates check)

2. **`teressa_books.py` line 133** — `books_sync` shim calls `sync_books` missing `date_range` arg
   - `books_sync(account_id, context)` calls `self.sync_books(account_id=account_id, context=context)`
   - `sync_books` requires: `(account_id, date_range, context)` — date_range is MISSING
   - Effect: `TypeError` whenever `books_sync` is called (Teressa books sync never works)
   - Fix: Either pass a default `date_range={}` or make `date_range` optional in `sync_books`

### Structural Issues (Non-Critical but Recurring)
- `docstring-after-methods` pattern: Multiple skillpacks (EliInbox, NoraConference, SarahFrontDesk, AdamResearch, TecDocuments) define compat wrapper methods BEFORE the class docstring. Not a runtime bug but violates PEP 257.
- `_activity_event_callback: callable | None` in adam_research.py — should be `Callable | None` (lowercase `callable` is valid but deprecated style)
- `_payroll_snapshots` global in milo_payroll.py — in-memory cross-request state, documented as Phase 1. Will cause tenant data leakage in long-running production deployment if suites share a process.

## Coverage Gaps
- No test exists that exercises the Eli quality gate path in `execute.py` — the CRITICAL `_deny_execution` NameError would never be caught by current tests
- No test for `teressa_books.books_sync` — shim signature mismatch is untested
- `_payroll_snapshots` global has no cleanup or TTL — process restart clears it

## Key Services Architecture
- `receipt_write_node`: DLP runs BEFORE chain hashing; YELLOW/RED require DLP (fail-closed)
- `kill_switch.py`: In-memory mode override + env var. Mode changes emit receipts.
- `token_mint_node`: `hmac.new()` used (valid Python 3 API)
- `token_service.py`: 6-check validation: signature, expiry, revocation, scope, suite_id, office_id
- `idempotency_service.py`: `check_and_reserve` / `mark_completed` / `mark_failed` — thread-safe
- `outbox_client.py`: Supabase backend when configured, in-memory fallback
- `exception_handler.py`: Uses `suite_id="system"` for exception receipts (prevents THREAT-002 cross-tenant poisoning — correct)

## Healthy Patterns Confirmed
- `routes/intents.py`: `allow_internal_routing` correctly derived server-side from `x-admin-token` header (not client payload) — THREAT-002 mitigated
- `correlation.py`: ContextVars properly reset in `finally` block — no leakage between requests
- `tec_documents.py`: Tenant isolation check on document_id (must start with suite_id/) — Law #6 enforced
- `mail_ops_desk.py`: `BLOCKED_CONTENT_FIELDS` frozenset prevents email body access — boundary enforced

## Pass 18 Lane 4 — Service Unit Tests (2026-04-30)
- 7 new test files: tests/services/test_{twilio_provisioning,elevenlabs_phone,sms_io}.py + tests/routes/test_{sarah_personalization,front_desk,telephony,sms_route}.py
- 65 total new tests, all passing. Coverage: twilio_provisioning 84%, elevenlabs_phone 83%, sms_io 84%.
- CRITICAL: Parallel agents (Lane 1, Lane 2) refactored the services DURING test writing:
  - `twilio_provisioning._idem_store` removed — replaced with persistent DB idempotency (`purchase_idempotency_key` column, migration 104)
  - All Twilio + EL HTTP calls wrapped with `resilient_call` (circuit breaker + retry) — 429/500 errors become `RetryableError` after exhaustion, NOT `TwilioProvisioningError`/`ElevenLabsPhoneError`
  - Test pattern for resilience errors: `pytest.raises((RetryableError, Exception))` + assert call count
- `test_kill_switch.py::TestKillSwitchReceiptPersistence::test_mode_change_persists_receipt` — PRE-EXISTING failure (0 receipts collected, unrelated to our changes)
- `tests/routes/` dir was NEW (did not exist) — Write tool creates it automatically

## File Writing in WSL
- `Write` tool uses Windows paths (C:\...) but WSL sees them at /mnt/c/...
- To create placeholder files for Edit tool: `wsl -d Ubuntu-22.04 -e bash -c "echo 'placeholder' > /mnt/c/..."`
- Read the placeholder file first before Edit (tool requirement)
- Cannot use heredoc for complex Python content in WSL bash -c (quote escaping breaks)

## Wave 2 Trust Hub Idempotency Tests (2026-05-03)
- New file: `orchestrator/tests/test_trust_state_machine_idempotency.py` — 12 passed, 1 skipped
- Source files must be extracted from `feat/per-tenant-trust-hub-w1-schema` branch to disk before pytest can import them (`.pyc`-only dirs are not discoverable by Python)
- Extract pattern: `git show feat/per-tenant-trust-hub-w1-schema:<path> > <disk_path>`
- Files extracted for test execution (not committed to wrong branch, remain untracked):
  `workers/trust_onboarding/state_machine.py`, `trust_receipts.py`, `cnam_sanitizer.py`, `__init__.py`, `worker.py`, `providers/twilio_trust_hub.py`, `workers/__init__.py`, `tests/test_trust_state_machine.py`
- `TrustReceiptError` constructor: `TrustReceiptError(code: str, message: str)` — two required args
- State machine idempotency guards: SID-column check in DB row BEFORE every Twilio create call
- 409 TrustHubError on assign_entity_to_profile/trust_product treated as idempotent success (not a failure)
- `_PatchContext` pattern from test_trust_state_machine.py is reusable but direct `patch()` context managers are cleaner for targeted idempotency tests
- skip reason: worker.py `advance_trust_state_task` import may fail if worker not on disk — guard with `pytest.skip`

## Wave 4+5 E2E Integration Test (2026-05-03)
- New file: `orchestrator/tests/test_trust_hub_e2e.py` — 2 passed
- `_DBSimulator` pattern: shared in-memory dict keyed by `(table, id)` lets route + state machine + callback share state without real DB
- `_ReceiptCapture.cut()`: wraps real `cut_trust_receipt` signature; captures receipt types + asserts PII guardrails inline
- Critical patching lesson: `state_machine.py` uses `from aspire_orchestrator.services.supabase_client import supabase_insert` INSIDE `_transition_shaken_approved()` function body — NOT at module level. Must patch `"aspire_orchestrator.services.supabase_client.supabase_insert"` NOT `patch.object(sm, "supabase_insert")` — the latter raises AttributeError.
- `mock_thub.create_end_user` side_effect: dispatch on `end_user_type` kwarg to return EU_SID_REP1 vs EU_SID_CNAM
- `mock_thub.create_trust_product` side_effect: dispatch on `policy_sid` kwarg to return SHAKEN_SID vs CNAM_SID
- `_seed_rep_with_eu_sid()`: after kyb_collected advance stores the rep row without eu_sid; must set it before profile_drafted advance or that handler returns `outcome="failed"` with MISSING_END_USER_SID
- `importlib.reload(sys.modules[sm_mod_name])` needed before each test to reset module-level policy SID cache (_POLICY_CACHE dict)
- TestClient with `raise_server_exceptions=False` required for webhook tests (status-callback returns 200 even on errors)

## Wave 7 A2P 10DLC Adversarial Tests (2026-05-04)
- 3 files: `test_a2p_state_machine_idempotency.py` (12), `test_a2p_state_machine.py` (+13 new = 44 total), `test_a2p_routes.py` (+6 new = 23 total). 79/79 pass.
- KEY FINDING: `_fail_brand` stores raw `str(exc)[:500]` as `rejection_reason` in DB. If Twilio error body contains a phone number (real scenario: rep verification error), it persists to DB. Receipt redacted_inputs/outputs are clean (PII check passes), but `rejection_reason` column is NOT checked by PII guard. REPORT — do not fix.
- `RetryableError` (from `aspire_orchestrator.services.resilience`) is NOT a `TrustHubError`. The state machine BLE001 guard catches it as UNHANDLED_EXCEPTION. ARQ retries the job (correct); but the brand row gets set to `rejected` on first failure. On retry the state is terminal. Bug: RetryableErrors should NOT mark brand as rejected — they should let ARQ retry without writing DB state. REPORT.
- PII audit: `_assert_no_pii` only checks `redacted_inputs` and `redacted_outputs` keys. It does NOT check `reason_message` or DB fields. The `rejection_reason` column is a PII leakage vector for Twilio error echoing.
- OTP replay: `submit_a2p_otp` does NOT guard on `brand_status == "otp_confirmed"`. A second OTP call when brand is already confirmed will call Twilio again (with same idempotency_key). Twilio deduplicates — not a security issue, but wastes a Twilio call.
- Use `_make_scope_for_tenant(suite_id, tenant_id, office_id)` pattern for cross-tenant isolation tests in routes.
- Receipt hash chain test requires patching `trust_receipts.supabase_select` AND `trust_receipts.supabase_insert` separately from the state machine patches (different module import paths).
- Campaign description `max_length=500` is enforced by Pydantic `Field(..., max_length=500)`.

## Wave 8 Trust Evil Tests (2026-05-04)
- New file: `orchestrator/tests/test_trust_evil.py` — 23 tests total
  - 9 skipped (live-DB: Class 1 x5 + Class 3 x4), 14 passed (programmatic: Class 2 x8 + Class 4 x3 + Class 5 x3)
- PII scan pattern: `_cut_with_pii_key(receipt_type, key, in_inputs)` iterates all RECEIPT_TYPES in a single test body — covers all 35 types per forbidden key. Much faster than parametrize.
- `teardown_method` for live-DB tests with immutable audit tables: DELETE via trust_profile cascade (can't DELETE transitions directly — immutability trigger blocks it). Trust profile DELETE cascades child rows via FK ON DELETE CASCADE.
- Class 5 (cap token tests): Do NOT patch `_validate_cap_token` — use real function + patch only DB/vault/receipt dependencies. This exercises the actual token_service logic.
- `asyncio.get_event_loop().run_until_complete(...)` works for sync test methods calling async functions (pytest not in async mode for these).
- SID injection tests: assert `mock_select.call_args_list` contains no injected string — stronger than just checking status code.
- Existing 351 trust tests pass unaffected (49 skipped = RLS tests without DB, unchanged).

## Links
- See `cycle5-bugs.md` for full detailed bug list
