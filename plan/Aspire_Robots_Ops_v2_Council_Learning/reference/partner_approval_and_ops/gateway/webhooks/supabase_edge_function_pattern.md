# Supabase Edge Function Pattern (Webhook)

Steps:
1) read raw body bytes
2) verify signature
3) extract provider event_id
4) call RPC: trust_ingest_webhook_event(provider, event_id, payload_redacted, trace_id)
5) emit receipt/event
6) return 200

Notes:
- never parse JSON before signature verification
- store only redacted payloads
