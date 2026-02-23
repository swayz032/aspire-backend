<!-- domain: pandadoc_api, subdomain: api_reference, chunk_strategy: heading_split -->

# PandaDoc API Reference

## Authentication

### API Key Authentication
PandaDoc uses API key authentication via the `API-Key` header. Do NOT use Bearer token format.

**Header format:**
```
Authorization: API-Key {your_api_key}
```

**Key types:**
- Live API keys: Prefix `api_` — used for production document generation
- Sandbox API keys: Used for testing — documents created are not legally binding
- Keys are workspace-scoped — one key per workspace

**Rate limits:**
- Standard plan: 60 requests/minute per workspace
- Business plan: 120 requests/minute per workspace
- Enterprise plan: Custom rate limits negotiated per contract
- Rate limit headers returned: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`
- 429 responses include `Retry-After` header (seconds)

### API Base URL
```
https://api.pandadoc.com/public/v1/
```

All endpoints use HTTPS. HTTP requests are rejected (not redirected).

## Documents API

### POST /documents — Create Document
Creates a new document from a template or from scratch.

**Required fields:**
- `name` (string): Document title displayed in PandaDoc dashboard
- `template_uuid` (string): UUID of the template to use, OR
- `editor` (boolean): Set `true` to create a blank document for editor

**Template-based creation fields:**
- `tokens` (array): Key-value pairs that replace template merge fields. Format: `[{"name": "Client.FirstName", "value": "John"}]`
- `fields` (object): Prefill form fields by API name. Format: `{"field_api_name": {"value": "content"}}`
- `content_placeholders` (array): Replace content placeholder blocks. Format: `[{"uuid": "placeholder-uuid", "blocks": [...]}]`
- `recipients` (array): Define signing roles. Each: `{"email", "first_name", "last_name", "role", "signing_order"}`
- `metadata` (object): Arbitrary key-value pairs stored with document

**Pricing table fields:**
- `pricing_tables` (array): Populate pricing/quote tables
- Each table: `{"name": "Pricing Table 1", "sections": [{"title": "Services", "rows": [...]}]}`
- Row format: `{"options": {"qty": 1, "price": "100.00"}, "data": {"Name": "Service"}}`

**Response (201 Created):**
```json
{
  "id": "document-uuid",
  "name": "Document Title",
  "status": "document.draft",
  "date_created": "2026-01-15T10:30:00.000Z",
  "uuid": "document-uuid"
}
```

**Error codes:**
- 400: Invalid template_uuid, missing required fields, malformed tokens
- 401: Invalid or missing API key
- 403: Workspace does not have access to template
- 429: Rate limit exceeded

### GET /documents/{id} — Read Document Details
Retrieves full document metadata, status, recipients, and field values.

**Path parameter:** `id` — Document UUID

**Response (200 OK):**
```json
{
  "id": "document-uuid",
  "name": "Document Title",
  "status": "document.draft",
  "date_created": "2026-01-15T10:30:00Z",
  "date_modified": "2026-01-15T11:00:00Z",
  "expiration_date": null,
  "version": "2",
  "recipients": [...],
  "tokens": [...],
  "fields": [...],
  "metadata": {...},
  "tags": [...]
}
```

**Document statuses:**
- `document.draft` — Created, not yet sent
- `document.sent` — Sent to recipients for review/signing
- `document.completed` — All recipients have signed
- `document.viewed` — At least one recipient has opened
- `document.waiting_approval` — Internal approval required
- `document.approved` — Internally approved
- `document.rejected` — Internally rejected
- `document.waiting_pay` — Payment collection pending
- `document.paid` — Payment collected
- `document.voided` — Document voided/cancelled
- `document.declined` — Recipient declined to sign
- `document.external_review` — Sent for external review

### GET /documents — List Documents
Returns paginated list of documents.

**Query parameters:**
- `page` (int, default 1): Page number
- `count` (int, default 15, max 100): Results per page
- `status` (string): Filter by status (e.g., `document.draft`)
- `tag` (string): Filter by tag
- `q` (string): Full-text search in document name
- `created_from` (ISO 8601): Filter by creation date start
- `created_to` (ISO 8601): Filter by creation date end
- `order_by` (string): Sort field (`date_created`, `date_modified`, `name`, `status`)

**Response (200 OK):**
```json
{
  "results": [...],
  "next": "https://api.pandadoc.com/public/v1/documents?page=2",
  "previous": null,
  "count": 150
}
```

### POST /documents/{id}/send — Send Document
Sends document to recipients for signing.

**Path parameter:** `id` — Document UUID

**Request body:**
```json
{
  "message": "Please review and sign this document",
  "subject": "Document for your signature",
  "silent": false
}
```

- `message` (string): Custom message in email notification
- `subject` (string): Custom email subject line
- `silent` (boolean): If `true`, send without email notification (use with embedded signing)

**Response (200 OK):**
```json
{
  "id": "document-uuid",
  "status": "document.sent"
}
```

**Preconditions:**
- Document must be in `document.draft` status
- All required fields must be filled
- At least one recipient must be assigned

### DELETE /documents/{id} — Delete Document
Permanently deletes a document. This action is irreversible.

**Response:** 204 No Content

**Restrictions:**
- Cannot delete completed documents
- Cannot delete documents with active signing sessions

### PATCH /documents/{id}/status — Change Document Status
Update document status (void, etc.).

**Request body:**
```json
{
  "status": "document.voided"
}
```

**Allowed transitions:**
- `document.sent` → `document.voided`
- `document.draft` → `document.voided`

## Documents Download

### GET /documents/{id}/download — Download Document
Downloads document as PDF.

**Query parameters:**
- `watermark` (boolean): Include watermark for draft documents
- `separate_files` (boolean): If document has multiple files, zip them

**Response:** Binary PDF content with `Content-Type: application/pdf`

## Templates API

### GET /templates — List Templates
Returns all templates in the workspace.

**Query parameters:**
- `page` (int): Page number
- `count` (int): Results per page (max 100)
- `tag` (string): Filter by tag
- `q` (string): Search in template name

**Response (200 OK):**
```json
{
  "results": [
    {
      "id": "template-uuid",
      "name": "Service Agreement",
      "date_created": "2026-01-10T09:00:00Z",
      "date_modified": "2026-01-12T14:30:00Z",
      "version": "3"
    }
  ]
}
```

### GET /templates/{id}/details — Template Details
Returns template structure including roles, tokens, fields, and content placeholders.

**Response (200 OK):**
```json
{
  "id": "template-uuid",
  "name": "Service Agreement",
  "tokens": [
    {"name": "Client.FirstName", "value": ""},
    {"name": "Sender.Company", "value": ""}
  ],
  "fields": [
    {"uuid": "field-uuid", "name": "client_address", "type": "text"},
    {"uuid": "field-uuid", "name": "start_date", "type": "date"}
  ],
  "roles": [
    {"uuid": "role-uuid", "name": "Client", "signing_order": 1},
    {"uuid": "role-uuid", "name": "Sender", "signing_order": 2}
  ],
  "content_placeholders": [
    {"uuid": "placeholder-uuid", "name": "scope_of_work", "block_id": "block-uuid"}
  ]
}
```

**Token naming conventions:**
- `Sender.*` — Tokens auto-filled from workspace settings
- `Client.*` — Tokens for document recipient
- `Custom.*` — Arbitrary tokens defined by template author
- Standard tokens: `Sender.Company`, `Sender.FirstName`, `Sender.LastName`, `Sender.Email`, `Client.FirstName`, `Client.LastName`, `Client.Email`, `Client.Company`

## Contacts API

### POST /contacts — Create Contact
Creates a contact in PandaDoc CRM.

**Request body:**
```json
{
  "email": "client@example.com",
  "first_name": "Jane",
  "last_name": "Doe",
  "company": "Doe Plumbing LLC",
  "phone": "+15551234567",
  "address": {
    "street": "123 Main St",
    "city": "Austin",
    "state": "TX",
    "zip": "78701"
  }
}
```

### GET /contacts — List Contacts
**Query parameters:** `page`, `count`, `q` (search)

## Folders API

### POST /documents/folders — Create Folder
### GET /documents/folders — List Folders
### PUT /documents/{id}/folders/{folder_id} — Move Document to Folder

## Error Response Format

All error responses follow this format:
```json
{
  "type": "error_type",
  "detail": "Human-readable error message",
  "status_code": 400
}
```

**Common error types:**
- `validation_error` — Invalid request body or parameters
- `authentication_error` — Invalid API key
- `permission_error` — Insufficient permissions
- `not_found` — Resource not found
- `rate_limit_error` — Rate limit exceeded
- `conflict_error` — Resource conflict (e.g., sending already-sent document)
- `server_error` — Internal server error (retry with backoff)

## Pagination Pattern

All list endpoints use cursor-based pagination:
```json
{
  "results": [...],
  "next": "https://api.pandadoc.com/public/v1/documents?page=2&count=15",
  "previous": null,
  "count": 250
}
```

- `next`: URL for next page (null if last page)
- `previous`: URL for previous page (null if first page)
- `count`: Total number of results across all pages
- Maximum `count` parameter: 100 per request

## Idempotency

PandaDoc does NOT natively support idempotency keys. To prevent duplicate document creation:
1. Generate a unique correlation ID client-side
2. Store in document metadata: `{"aspire_correlation_id": "uuid"}`
3. Before creating, search existing documents by metadata
4. Use the stored correlation ID for deduplication

## Content Types

- Request bodies: `application/json`
- File uploads: `multipart/form-data`
- Responses: `application/json` (except downloads which return binary)

## Timezone Handling

All timestamps in PandaDoc API responses are in UTC (ISO 8601 format with Z suffix). When displaying to users, convert to their local timezone. When creating documents with date fields, specify dates in ISO 8601 format.
