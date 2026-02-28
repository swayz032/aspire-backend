# Support Playbook: Payroll Run Failed

Steps:
1) Inspect receipt + provider_call_log (redacted)
2) If retryable: retry outbox job with same idempotency key
3) If nonretryable: create A2A item for human resolution
4) Notify customer with next steps
