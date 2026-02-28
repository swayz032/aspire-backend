# Incident Operations Specification

**Source:** Ava Admin Enterprise Handoff v2

## First 5 Minutes (Operator Mode)

1. **Confirm impact** - Are users blocked? Which feature? Which suites? Check Incidents page and note incident_id.
2. **Correlate** - Open Service Calls (Provider Logs) for the impacted provider. Open Proof Log (Receipts) for the correlation_id.
3. **Check rollouts** - If incident started after a rollout, pause or rollback canary (requires approval if not green).
4. **Stabilize** - Prefer reversible mitigations: pause rollout, enable provider outage mode, throttle retries / shed load.
5. **Evidence pack** - Capture receipt_ids + provider_call_ids + trace_ids for later postmortem.

## Idempotency Policy

### Required
- Every side effect must have an idempotency key.
- Outbox job stores idempotency key.
- Provider call log dedupes on (provider, idempotency_key).

### Key format
`<suite_id>:<job_id>:<action_type>:<hash(payload_normalized)>`

### Payload normalization
- stable key order
- remove non-deterministic fields
- canonicalize money amounts (cents + currency)

## Provider Error Taxonomy (Stable)

### Classes
- **retryable:** transient (timeouts, 5xx, rate-limit)
- **nonretryable:** permanent but safe (validation, insufficient funds, invalid account)
- **fatal:** security/authorization, schema corruption, signature invalid storms

### Stable codes
- RATE_LIMITED
- TIMEOUT
- VENDOR_5XX
- VENDOR_4XX_VALIDATION
- AUTH_INVALID
- AUTH_SCOPE_INSUFFICIENT
- WEBHOOK_SIGNATURE_INVALID
- WEBHOOK_REPLAY_DETECTED
- DUPLICATE_IDEMPOTENCY_KEY
- TRANSFER_AUTHORIZATION_DECLINED
- TRANSFER_RETURNED
- PAYROLL_RUN_BLOCKED
- PAYROLL_BANK_VERIFICATION_REQUIRED

### Rule
Adapter returns stable code + a redacted vendor code in metadata (never secrets/PII).

## Cross-reference
- IncidentPacket schema: `plan/contracts/ava-admin/incident_packet.schema.json`
- OpsExceptionCard schema: `plan/contracts/ava-admin/ops_exception_card.schema.json`
- Implementation target: Phase 3+ (Incident Lifecycle)
