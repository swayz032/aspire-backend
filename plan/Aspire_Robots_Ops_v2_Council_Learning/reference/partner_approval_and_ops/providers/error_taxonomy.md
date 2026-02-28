# Provider Error Taxonomy (Stable)

## Classes
- retryable: transient (timeouts, 5xx, rate-limit)
- nonretryable: permanent but safe (validation, insufficient funds, invalid account)
- fatal: security/authorization, schema corruption, signature invalid storms

## Suggested stable codes
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

## Rule
Adapter returns stable code + a redacted vendor code in metadata (never secrets/PII).
