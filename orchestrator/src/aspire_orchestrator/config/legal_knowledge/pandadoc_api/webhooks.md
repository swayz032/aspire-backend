<!-- domain: pandadoc_api, subdomain: webhooks, chunk_strategy: heading_split -->

# PandaDoc Webhooks

## Webhook Configuration

### Setting Up Webhooks
Webhooks are configured in PandaDoc workspace settings under Integrations > Webhooks, or via API.

**Webhook URL requirements:**
- Must be HTTPS (HTTP endpoints rejected)
- Must respond within 10 seconds
- Must return 2xx status code to acknowledge receipt
- Failed deliveries retry up to 10 times with exponential backoff

### Webhook Shared Key
Each workspace has a shared key for HMAC-SHA256 signature verification. The key is displayed once during setup and must be stored securely.

## Webhook Events

### document_state_changed
Fires when a document transitions between statuses.

**Payload:**
```json
{
  "event": "document_state_changed",
  "data": {
    "id": "document-uuid",
    "name": "Service Agreement - Doe Plumbing",
    "status": "document.sent",
    "date_created": "2026-01-15T10:30:00Z",
    "date_modified": "2026-01-15T11:00:00Z"
  }
}
```

**Status transitions that trigger this event:**
- `document.draft` -> `document.sent`
- `document.sent` -> `document.completed`
- `document.sent` -> `document.voided`
- `document.sent` -> `document.declined`
- `document.draft` -> `document.voided`
- `document.waiting_approval` -> `document.approved`
- `document.waiting_approval` -> `document.rejected`

### recipient_completed
Fires when an individual recipient completes their signing action.

**Payload:**
```json
{
  "event": "recipient_completed",
  "data": {
    "id": "document-uuid",
    "name": "Service Agreement",
    "status": "document.sent",
    "recipient": {
      "email": "client@example.com",
      "first_name": "Jane",
      "last_name": "Doe",
      "role": "Client",
      "completed_at": "2026-01-15T14:30:00Z"
    }
  }
}
```

**Use cases:**
- Track individual signer progress in multi-party contracts
- Send confirmation to specific recipients
- Update contract state machine (SENT -> partial signing in progress)

### document_completed
Fires when ALL recipients have completed their actions and the document is fully executed.

**Payload:**
```json
{
  "event": "document_completed",
  "data": {
    "id": "document-uuid",
    "name": "Service Agreement",
    "status": "document.completed",
    "date_completed": "2026-01-15T15:00:00Z",
    "recipients": [
      {"email": "sender@company.com", "role": "Sender", "completed_at": "2026-01-15T14:00:00Z"},
      {"email": "client@example.com", "role": "Client", "completed_at": "2026-01-15T15:00:00Z"}
    ]
  }
}
```

**This is the definitive event for contract completion.** Use this to transition contract state to SIGNED.

### document_viewed
Fires when a recipient opens the document for the first time.

**Payload:**
```json
{
  "event": "document_viewed",
  "data": {
    "id": "document-uuid",
    "name": "Service Agreement",
    "recipient": {
      "email": "client@example.com",
      "first_name": "Jane",
      "last_name": "Doe",
      "viewed_at": "2026-01-15T12:00:00Z"
    }
  }
}
```

### document_creation_failed
Fires when async document creation from template fails.

**Payload:**
```json
{
  "event": "document_creation_failed",
  "data": {
    "id": "document-uuid",
    "name": "Service Agreement",
    "error": "Template field mismatch",
    "error_code": "template_error"
  }
}
```

### document_deleted
Fires when a document is permanently deleted.

### document_updated
Fires when document content or metadata is modified.

## HMAC-SHA256 Signature Verification

### Signature Header
Each webhook request includes the header `X-PandaDoc-Signature` containing the HMAC-SHA256 hash of the request body.

### Verification Process
```python
import hmac
import hashlib

def verify_webhook(payload_bytes: bytes, signature: str, shared_key: str) -> bool:
    expected = hmac.new(
        shared_key.encode('utf-8'),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

**Verification rules:**
1. Always verify before processing — fail closed (Aspire Law #3)
2. Use constant-time comparison (`hmac.compare_digest`) to prevent timing attacks
3. Verify against raw request body bytes, not parsed JSON (whitespace/encoding matters)
4. Log verification failures with denial receipts

### Aspire Integration Pattern
```python
# In webhook handler:
signature = request.headers.get('X-PandaDoc-Signature', '')
if not verify_webhook(request.body, signature, PANDADOC_WEBHOOK_KEY):
    emit_denial_receipt(event_id, reason='hmac_verification_failed')
    return 401
```

## Delivery & Retry Behavior

### Retry Schedule
Failed webhook deliveries (non-2xx response or timeout) retry with exponential backoff:
- Attempt 1: Immediate
- Attempt 2: 1 minute
- Attempt 3: 5 minutes
- Attempt 4: 30 minutes
- Attempt 5: 2 hours
- Attempts 6-10: 6 hours each

After 10 failed attempts, the webhook is disabled and workspace admins are notified.

### Idempotency
Webhooks may be delivered more than once. Every webhook payload includes an event ID. Use this for idempotent processing:
1. Store processed event IDs in `processed_webhooks` table
2. Before processing, check if event ID exists
3. If duplicate, acknowledge with 200 but skip processing
4. Use database unique constraint on event_id for race condition safety

### Ordering
Webhook delivery order is NOT guaranteed. A `document_completed` event may arrive before `recipient_completed`. Design handlers to be order-independent.

### Timeout
PandaDoc waits 10 seconds for webhook response. If your processing takes longer:
1. Acknowledge immediately with 200
2. Process asynchronously (queue the event)
3. Store raw payload for reprocessing if needed

## Testing Webhooks

### Sandbox Environment
Use sandbox API keys to trigger test webhooks. Documents created in sandbox mode send real webhook events but are marked as non-binding.

### Manual Testing
PandaDoc dashboard includes a "Test Webhook" button that sends a sample `document_state_changed` event to verify endpoint connectivity.

### Webhook Logs
PandaDoc dashboard shows recent webhook delivery attempts, response codes, and response times under Integrations > Webhooks > Delivery Log.
