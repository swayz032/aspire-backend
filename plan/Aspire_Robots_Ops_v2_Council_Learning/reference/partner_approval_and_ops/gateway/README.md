# Gateway Webhooks (Scaffolds)

Implement provider webhooks as a single, audited, idempotent boundary:
1) Verify signature on raw bytes
2) Deduplicate by (provider, event_id)
3) Emit receipt/event (redacted)
4) Update dependent state via Trust Spine RPCs
