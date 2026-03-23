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

## Links
- See `cycle5-bugs.md` for full detailed bug list
