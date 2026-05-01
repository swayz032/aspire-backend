---
name: Resilience Patterns (Pass 18+ Lane 2)
description: Reusable circuit breaker + retry primitives for external HTTP calls in services/resilience.py
type: project
---

# Resilience patterns for external HTTP

Module: `backend/orchestrator/src/aspire_orchestrator/services/resilience.py`

Why: Pass 18+ Lane 2 production hardening — every external HTTP call (Twilio, ElevenLabs, Supabase) must be wrapped with circuit breaker + retry to satisfy Law #10 reliability gate.

How to apply:

- New external integration → import `resilient_call`, `RetryPolicy`, `AsyncCircuitBreaker` from `aspire_orchestrator.services.resilience`. Register a module-level breaker in resilience.py if it's a new provider.
- Default policies: `TWILIO_RETRY` (3 attempts, 12s budget), `ELEVENLABS_RETRY` (same), `SUPABASE_RETRY` (2 attempts, 180ms budget for personalization-style hot paths).
- `idempotent=True` retries on `RetryableError` + network errors. Use for GET/PUT/PATCH/DELETE that are naturally idempotent.
- `idempotent=False` retries ONLY on network errors (the remote never saw the request). Use for POST that has a side effect (purchase, send SMS, place call).
- Raise `RetryableError("CODE", "msg")` from your wrapped function on transient failures (5xx, 429). 4xx errors should be raised as your domain-specific error so they don't trip the breaker.

Persistent idempotency for non-idempotent POSTs:

- DB column: `tenant_phone_numbers.purchase_idempotency_key` + partial UNIQUE index on `(suite_id, purchase_idempotency_key)`.
- Lookup BEFORE the POST. If found, return cached row (idempotent replay).
- INSERT with key set AFTER the POST succeeds — UNIQUE catches concurrent races; rollback Twilio side and re-read.
- Never use in-memory dicts for idempotency state — pod restart loses it.

Twilio Idempotency-Key header:

- `Idempotency-Key` HTTP header on Twilio Messages POST forces server-side dedup. We compute SHA256(thread_memory_id||body||minute_bucket) and pass it both to our DB and to Twilio.

Sarah personalization budget pattern (latency-critical webhooks):

- Total budget: 800ms. Per-query: 200ms. `asyncio.wait_for` at both levels.
- LKG cache: in-memory `OrderedDict` keyed by called_number, 256 entries, 10min TTL.
- Always cut a `personalization_cache_fallback` receipt when fallback fires (Law #2 — degraded paths are still state changes).
- Return `_aspire_fallback: true` in response so downstream tooling knows.
