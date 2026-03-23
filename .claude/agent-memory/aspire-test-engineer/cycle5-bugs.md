# Cycle 5 Full Sweep Bug Report (2026-03-22)

## Files Scanned
All ~150+ Python files in `orchestrator/src/aspire_orchestrator/`:
- nodes/: execute.py, approval_check.py, policy_eval.py, token_mint.py, receipt_write.py, intake.py, agent_reason.py, safety_gate.py, param_extract.py
- services/: tool_executor.py, idempotency_service.py, approval_service.py, dlp.py, policy_engine.py, supabase_client.py, receipt_store.py, kill_switch.py, a2a_service.py, outbox_client.py, skillpack_factory.py, llm_router.py, token_service.py
- skillpacks/: all 20 files
- routes/: intents.py, admin.py, webhooks.py, robots.py
- middleware/: exception_handler.py, correlation.py, chaos.py, rate_limiter.py
- config/settings.py, graph.py

## CRITICAL Bugs

### BUG-C1: execute.py — _deny_execution used before definition
- File: `nodes/execute.py`, line 266 (first use), line 312 (definition)
- Category: Dead code / control flow error
- Description: The nested function `_deny_execution()` is called at line 266 inside the `execute_node()` function body, before its `def` statement at line 312. Python does NOT hoist nested function definitions — the `def` must execute before the name is bound.
- Trigger: `assigned_agent == "eli"` AND `task_type in ("email.draft", "email.send")` AND `state.get("execution_params")` is not a dict
- Effect: `NameError: name '_deny_execution' is not defined` — full node crashes, pipeline halts
- Law Violated: Law #3 (Fail Closed) — should fail closed with a receipt, instead raises unhandled NameError
- Fix: Move the `def _deny_execution(reason_code, message)` block (lines 312-340) to BEFORE line 263 (before the Eli gates comment), so the function is defined before it can be called.

### BUG-C2: teressa_books.py — books_sync missing required date_range argument
- File: `skillpacks/teressa_books.py`, line 133
- Category: Type mismatch / signature error
- Description: The `books_sync` compatibility wrapper calls `self.sync_books(account_id=account_id, context=context)`, but `sync_books` at line 166 requires three positional arguments: `account_id`, `date_range`, `context`. The `date_range` argument is never passed.
- Effect: `TypeError: sync_books() missing 1 required positional argument: 'context'` at runtime — `date_range` is consumed as `context`, and `context` is missing.
- Law Violated: Law #2 (Receipt for All) — error throws before receipt is emitted
- Fix option A: Make `date_range` optional in `sync_books`: `date_range: dict[str, str] | None = None`
- Fix option B: Add a default in the shim: `return await self.sync_books(account_id=account_id, date_range={}, context=context)`

## HIGH Issues

### BUG-H1: token_mint.py — hmac.new() (style note, not a bug)
- File: `nodes/token_mint.py`, line 98
- Category: Code style
- Description: Uses `hmac.new(key, msg, digestmod)` which is the older but still valid Python API. Not a bug.
- Status: CONFIRMED NOT A BUG — `hmac.new()` is valid Python 3. No action needed.

### BUG-H2: milo_payroll.py — process-global _payroll_snapshots
- File: `skillpacks/milo_payroll.py`
- Category: Tenant isolation concern (Phase 1 in-memory)
- Description: `_payroll_snapshots: dict[str, Any] = {}` is a module-level dict keyed by `f"{suite_id}:{payroll_period}"`. In a long-running production process, this accumulates indefinitely and is never cleaned. If the process handles multiple tenants, all their payroll snapshot data lives in the same process memory.
- Risk: Memory exhaustion over time; no TTL; data persists across requests
- Law Concern: Law #6 (Tenant Isolation) — data is keyed by suite_id (correct), but no TTL/eviction means leaked memory and potential data disclosure if process is forked
- Severity: HIGH for production, LOW for current dev phase
- Fix: Add TTL eviction or move to Supabase storage

### BUG-H3: admin.py — _incidents, _provider_calls, _rollouts are in-memory singletons
- File: `routes/admin.py`, lines 67-70
- Category: Fail-open in-memory fallback
- Description: In-memory stores are used as fallback when Supabase is unavailable. These are module-level dicts with no size cap, no TTL, and no persistence across restarts. Thread lock (`_store_lock`) is present.
- Risk: Data loss on restart, unbounded memory growth
- Severity: HIGH (documented as Phase 1 fallback)

## WARNING Issues

### BUG-W1: Multiple skillpacks — class docstring after method definitions
- Files: eli_inbox.py (line 278), nora_conference.py (line 135), sarah_front_desk.py (line 126), adam_research.py (line 138), tec_documents.py (line 183)
- Category: Dead code / structural
- Description: The class-level docstring (triple-quoted string) appears AFTER several method definitions. In Python, a string literal after method definitions in a class body is NOT the class docstring — it's a standalone string expression that is parsed but ignored. The actual class docstring (if any) should be the FIRST statement after `class Foo:`.
- Effect: `ClassName.__doc__` will be `None` for these classes. Any tooling that relies on class docstrings (help(), pydoc, IDE tooltips) will show no documentation.
- Law Concern: None — functional behavior is unaffected
- Fix: Move the triple-quoted string to immediately follow the `class Foo:` line

### BUG-W2: adam_research.py — callable type annotation
- File: `skillpacks/adam_research.py`, line 46
- Category: Type annotation style
- Description: `_activity_event_callback: callable | None = None` uses lowercase `callable` as a type annotation. While syntactically valid (callable is a builtin), the correct modern typing is `Callable | None` from `typing` or `collections.abc`.
- Severity: Low — no runtime impact, but will cause mypy/pyright warnings

### BUG-W3: skillpack_factory.py — file open without explicit encoding
- File: `services/skillpack_factory.py`, line 72
- Category: Portability
- Description: `with open(self._manifest_path) as f:` — no `encoding="utf-8"` specified. On Windows environments with non-UTF-8 default locale, YAML files with non-ASCII characters will decode incorrectly.
- Fix: `with open(self._manifest_path, encoding="utf-8") as f:`

### BUG-W4: ava_user.py — AvaUserSkillPack class has no body
- File: `skillpacks/ava_user.py`, lines 13-14
- Category: Dead code
- Description: `class AvaUserSkillPack(AgenticSkillPack):` is defined with no body — the next line immediately starts `class EnhancedAvaUser(AgenticSkillPack):`. This makes `AvaUserSkillPack` an empty class with inherited methods only. If it's intended to be a base or alias, it should at minimum have `pass` or a docstring. If it's unused, it should be removed.

### BUG-W5: finn_finance_manager.py — read_finance_exceptions is synchronous but class is async
- File: `skillpacks/finn_finance_manager.py`, lines 223-253
- Category: Async inconsistency
- Description: `read_finance_exceptions()` is defined as a regular synchronous function (not `async def`), but all other methods in the module are `async def`. The `EnhancedFinnFinanceManager.finance_exceptions_read` wrapper at line 466 calls it with a direct call (not `await`), which is correct for a sync function. However, this inconsistency could confuse developers who assume all skill pack operations are async.
- Severity: Low — functionally correct as long as callers don't accidentally `await` it

### BUG-W6: graph.py — classify_node mutates intent_result dict in-place
- File: `graph.py`, lines 410-416 and 440-442
- Category: State mutation
- Description: `intent_result.action_type = "email.draft"` (line 411) mutates the Pydantic model in-place directly on the object attribute. The result dict `result["intent_result"]` is then set from `intent_result.model_dump()` (line 419). Later at line 440, `result["intent_result"]["action_type"] = explicit_task_type` mutates the serialized dict directly. This is inconsistent — mixing object mutation and dict mutation on related data.
- Risk: If `intent_result` is shared reference (it isn't in the current flow), mutations would propagate. Currently safe but fragile.

## INFO / Previously Fixed (Not Re-Reported)

The following categories were checked and found CLEAN (or already addressed in Cycles 1-4):
- Broken imports: None detected across all scanned files
- Hardcoded secrets: None — all credentials via `settings.*` from env vars
- Raw SQL injection surfaces: None — all DB access via parameterized Supabase client
- Missing receipt for policy denials: Receipt emitted in all denial paths checked
- Cross-tenant reads without suite_id: All DB queries pass suite_id
- Webhook signature bypass: Stripe and PandaDoc handlers verified
- Token signing key fail-closed: Confirmed — raises ValueError if key is "UNCONFIGURED-FAIL-CLOSED"
- Correlation ID CRLF injection: Fixed in correlation.py (THREAT-001)
- Admin token server-side derivation: Confirmed — intents.py line 344 derives from header, not payload (THREAT-002)
- Exception handler cross-tenant: Confirmed — uses suite_id="system" for exception receipts
