# Webhook Signature Verification Checklist

- verify raw body bytes (do not JSON-parse before verify)
- enforce timestamp tolerance (clock skew)
- reject invalid signature (401) and emit security receipt/event
- dedupe provider event_id (replay protection)
- rate limit endpoint
- redact payload before logging
