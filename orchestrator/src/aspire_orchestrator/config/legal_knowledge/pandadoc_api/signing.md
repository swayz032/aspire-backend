<!-- domain: pandadoc_api, subdomain: signing, chunk_strategy: heading_split -->

# PandaDoc Signing & E-Signature

## Signing Session Types

### Embedded Signing Sessions
Embed the PandaDoc signing experience within your application using an iframe. The signer never leaves your app.

**Creating a session:**
```
POST /documents/{id}/session
```

**Request body:**
```json
{
  "recipient": "client@example.com",
  "lifetime": 900
}
```

- `recipient` (string, required): Email of the recipient who will sign
- `lifetime` (integer, optional): Session duration in seconds (default 900, max 86400)

**Response (201 Created):**
```json
{
  "id": "session-uuid",
  "session_id": "session-uuid",
  "expires_at": "2026-01-15T11:15:00Z"
}
```

### Signing URL Construction
After creating a session, construct the signing URL:
```
https://app.pandadoc.com/s/{session_id}
```

Embed this URL in an iframe for in-app signing.

### Session Expiration
- Default lifetime: 900 seconds (15 minutes)
- Maximum lifetime: 86400 seconds (24 hours)
- After expiration, a new session must be created
- Creating a new session invalidates any previous active sessions for the same recipient

### Session Security
- Sessions are single-use: once a signer completes, the session cannot be reused
- Sessions are recipient-specific: only the designated recipient can sign
- Session URLs should never be shared publicly or cached
- PandaDoc validates the session server-side on every page load

## Signing Workflow

### Standard Email-Based Signing
1. Create document from template (POST /documents)
2. Send document (POST /documents/{id}/send)
3. Recipients receive email with signing link
4. Each recipient signs in order (if signing_order is set)
5. Webhook `recipient_completed` fires after each signature
6. Webhook `document_completed` fires when all recipients have signed

### Embedded Signing Workflow
1. Create document from template (POST /documents)
2. Send document with `silent: true` (no email notification)
3. Create signing session (POST /documents/{id}/session)
4. Embed session URL in iframe
5. Signer completes within iframe
6. Webhook `recipient_completed` fires
7. If more signers, create new session for next recipient
8. Webhook `document_completed` fires when all done

### Aspire Integration: External Signing Page
For Aspire's external signing flow:
1. Clara generates contract via PandaDoc API
2. Contract saved to Aspire contracts table with state SENT
3. Signing session created per recipient
4. Aspire generates public signing URL: `{BASE_URL}/sign/{signing_token}`
5. External page loads, fetches session from Aspire API (token-based, no auth)
6. PandaDoc signing iframe embedded in Aspire-branded page
7. On completion, webhook updates contract state to SIGNED

## Signing Order

### Sequential Signing
Set `signing_order` on recipients to enforce signing sequence:
```json
{
  "recipients": [
    {"email": "manager@company.com", "role": "Sender", "signing_order": 1},
    {"email": "client@example.com", "role": "Client", "signing_order": 2}
  ]
}
```

Recipient 2 cannot sign until Recipient 1 has completed. PandaDoc enforces this server-side.

### Parallel Signing
Omit `signing_order` or set same value for all recipients to allow parallel signing. All recipients can sign simultaneously.

## E-Signature Legal Validity

### ESIGN Act Compliance
PandaDoc e-signatures comply with the Electronic Signatures in Global and National Commerce Act (ESIGN, 2000):
- Signer intent demonstrated by explicit click-to-sign action
- Consent to electronic records captured before signing
- Record retention: PandaDoc stores signed documents indefinitely
- Association: signature attached to specific document version

### UETA Compliance
PandaDoc also complies with the Uniform Electronic Transactions Act (UETA), adopted by 49 states (all except New York, which has its own Electronic Signatures and Records Act — ESRA):
- Electronic signature has same legal effect as wet ink
- Electronic record satisfies writing requirement
- Attribution via email verification and audit trail

### Audit Trail
Every PandaDoc document includes a Certificate of Completion containing:
- Signer identity (name, email)
- IP address at time of signing
- Timestamp of each action (viewed, signed)
- Document hash for tamper detection
- Signing method (click-to-sign, draw, type, upload)

### Exceptions — When E-Signatures Are NOT Valid
Certain document types require wet-ink signatures by law:
- Wills and testamentary trusts
- Family law documents (adoption, divorce in some states)
- Court orders and notices
- Utility service cancellation notices (some states)
- Health insurance termination notices
- Product recall notices
- Documents governed by UCC Articles 3-9 (negotiable instruments)

## Signature Types in PandaDoc

### Click-to-Sign
Signer clicks a button to apply a computer-generated signature. Fastest and most common.

### Draw Signature
Signer draws their signature using mouse or touch. Stored as image.

### Type Signature
Signer types their name, displayed in a signature font. Multiple font options available.

### Upload Signature
Signer uploads an image of their signature. Image is embedded in document.

### Initials
For documents requiring initials on each page, PandaDoc supports initial fields alongside signature fields.

## Signing Field Types

### Signature Field
Primary signing action. Placed in template via drag-and-drop editor.

### Date Signed Field
Auto-populated with the date when signature is applied. Cannot be manually overridden by signer.

### Text Input Field
Free-text input for signer to fill (e.g., title, company name).

### Checkbox Field
Boolean toggle for terms acceptance, optional clauses, etc.

### Initials Field
For page-by-page initialing requirements.

### Dropdown Field
Predefined options for signer selection.

## Signing Status Tracking

### Per-Recipient Status
- `awaiting` — Has not yet opened document
- `opened` — Opened but not signed
- `completed` — Signature applied
- `declined` — Actively declined to sign

### Document-Level Status
- `document.sent` — At least one recipient has not completed
- `document.completed` — All recipients have completed
- `document.declined` — At least one recipient declined

## Decline Flow
When a recipient declines:
1. They can optionally provide a decline reason
2. Document status changes to `document.declined`
3. Webhook `document_state_changed` fires with declined status
4. Other recipients are notified
5. Document cannot be completed — must create new document if needed

## Revocation / Voiding
Sender can void a sent document at any time before completion:
```
PATCH /documents/{id}/status
{"status": "document.voided"}
```
- All pending signing sessions are immediately invalidated
- Recipients are notified of voiding
- Document cannot be un-voided — must create new document
