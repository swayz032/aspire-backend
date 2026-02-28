# Idempotency Policy

## Required
- Every side effect must have an idempotency key.
- Outbox job stores idempotency key.
- Provider call log dedupes on (provider, idempotency_key).

## Key format
<suite_id>:<job_id>:<action_type>:<hash(payload_normalized)>

## Payload normalization
- stable key order
- remove non-deterministic fields
- canonicalize money amounts (cents + currency)
