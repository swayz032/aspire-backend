---
name: Pass 19 Messages Route Patterns
description: Key implementation patterns, gotchas, and decisions from Pass 19 Lane E1 (messages API + migration 107)
type: project
---

## FastAPI dict Query param workaround

FastAPI cannot accept `dict[str, Any] | None = Query(None)` — raises AssertionError at startup.

**Fix:** Use `str | None = Query(None)` and parse with `json.loads()` inside the route:

```python
def _parse_capability_token_param(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    import json
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None
```

PATCH routes receive token in request body (Pydantic model) — no issue there.

**Why:** FastAPI scalar check for Query params rejects complex types.

**How to apply:** Any future GET route that needs a dict param (capability_token, filters, etc.) must use str param + json.loads parsing.

---

## sms_thread is NOT a separate table

`sms_thread` is `memory_objects` rows with `memory_type='sms_thread'`. There is no separate `sms_thread` table. Migration 102 only adds `sms_messages` (individual messages), `tenant_phone_numbers`, `front_desk_configs`, `front_desk_routing_contacts`.

**Why:** The memory spine design uses `memory_objects` as a universal projection for all entity types.

**How to apply:** Any future query on SMS threads must use `memory_objects` table with `memory_type=eq.sms_thread` filter. State columns (`is_pinned`, `is_archived`, `read_at`) were added by migration 107 directly on `memory_objects`.

---

## supabase_select filter syntax (PostgREST)

The `filters` param is a raw PostgREST query string. Complex compound filters:

```python
# OR clause (PostgREST syntax)
"&or=(recommended_action.eq.sms_reply_needed,recommended_action.eq.sms_followup)"

# IS NULL
"&read_at=is.null"

# Boolean equality
"&is_pinned=eq.true"

# Date range
"&last_activity_at=gte.<ISO_DATE>"
```

**Key:** URL-encode dynamic values with `urllib.parse.quote(value, safe='')`.

---

## proactive_candidates action column name

The column is `recommended_action` (not `action`). Verified from schema query.

SMS-relevant actions: `sms_reply_needed`, `sms_followup`. Status filter: `status=eq.open`.

---

## Gateway PATCH proxy pattern

`proxyToOrchestrator` only accepts `'GET' | 'POST' | 'PUT' | 'DELETE'` — no PATCH.

**Fix:** Use direct `fetch()` with `method: 'PATCH'` for PATCH endpoints. Pattern:

```typescript
async function patchToOrchestrator(req, res, path) {
  const url = `${ORCHESTRATOR_BASE_URL}${path}`;
  const response = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'X-Suite-Id': suiteId, ... },
    body: JSON.stringify(req.body),
    signal: controller.signal,
  });
  res.status(response.status).json(await response.json());
}
```

---

## Receipt cutting in route handlers

Use `receipt_store.store_receipts([{ ... }])` (synchronous, non-blocking). The store has in-memory + async Supabase write.

Key fields: `id`, `suite_id`, `office_id`, `tenant_id`, `correlation_id`, `trace_id`, `receipt_type`, `action_type`, `tool_used`, `risk_tier`, `capability_token_id`, `actor_type`, `outcome`, `redacted_inputs`, `redacted_outputs`.

---

## Toggle pattern for pin/archive

Routes that toggle boolean state must:
1. Read current value with `supabase_select` (1 row)
2. Flip it
3. Write with `supabase_update`

Test pattern: mock `supabase_select` to return `[{is_pinned: False}]` before patching `_update_thread_state`.
