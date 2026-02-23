<!-- domain: pandadoc_api, subdomain: merge_fields, chunk_strategy: heading_split -->

# PandaDoc Merge Fields

## Three Field Mechanisms

PandaDoc has three distinct mechanisms for injecting dynamic content into templates. Understanding the difference is critical for correct document generation.

### 1. Tokens (Template Variables)
Tokens are simple text replacement placeholders defined in templates. They appear as `{{Token.Name}}` in the template editor.

**API field:** `tokens` (array of `{name, value}` objects)

**How they work:**
- Template author places tokens in document text
- API caller provides values at document creation time
- Values are plain text — no formatting, no rich content
- If a token is not provided, it renders as empty string (no error)

**Standard token names (auto-populated from workspace settings):**
- `Sender.Company` — Workspace company name
- `Sender.FirstName` — Document creator's first name
- `Sender.LastName` — Document creator's last name
- `Sender.Email` — Document creator's email
- `Sender.Phone` — Document creator's phone
- `Sender.Address` — Workspace address
- `Sender.Title` — Document creator's title

**Recipient tokens (auto-populated from recipient data):**
- `Client.FirstName` — Recipient's first name
- `Client.LastName` — Recipient's last name
- `Client.Email` — Recipient's email
- `Client.Company` — Recipient's company

**Custom tokens (defined per template):**
- `Custom.ProjectName`
- `Custom.ContractValue`
- `Custom.StartDate`
- Any arbitrary name with `Custom.` prefix

**Aspire usage pattern:**
```json
{
  "tokens": [
    {"name": "Client.FirstName", "value": "Jane"},
    {"name": "Client.LastName", "value": "Doe"},
    {"name": "Client.Company", "value": "Doe Plumbing LLC"},
    {"name": "Custom.ContractValue", "value": "$15,000"},
    {"name": "Custom.StartDate", "value": "March 1, 2026"}
  ]
}
```

### 2. Fields (Form Fields)
Fields are interactive form elements that can be pre-filled via API or completed by signers during the signing process.

**API field:** `fields` (object mapping field API names to values)

**Field types:**
- `text` — Free-text input
- `date` — Date picker (ISO 8601 format for API, rendered as locale-formatted)
- `number` — Numeric input
- `checkbox` — Boolean (true/false)
- `dropdown` — Select from predefined options
- `signature` — Signing field (not pre-fillable)
- `initials` — Initials field (not pre-fillable)

**How fields differ from tokens:**
- Fields are interactive — signers can modify them during signing
- Fields have validation (required, format, allowed values)
- Fields are tied to specific roles (Sender field, Client field)
- Fields appear in the field panel during signing
- Tokens are static text replacement — invisible to signers

**API prefill pattern:**
```json
{
  "fields": {
    "client_address": {"value": "123 Main St, Austin TX 78701"},
    "start_date": {"value": "2026-03-01"},
    "payment_terms": {"value": "Net 30"}
  }
}
```

**Role-specific field assignment:**
Each field in a template is assigned to a role. Only the recipient in that role can edit the field during signing. Fields assigned to `Sender` are typically pre-filled via API.

### 3. Content Placeholders (Rich Content Blocks)
Content placeholders are named regions in a template where structured content blocks can be injected.

**API field:** `content_placeholders` (array of placeholder objects)

**How they work:**
- Template author creates a named content placeholder region
- API caller provides rich content blocks to fill the region
- Supports paragraphs, headings, lists, tables, and images
- Content replaces the placeholder entirely

**Block types:**
```json
{
  "content_placeholders": [
    {
      "uuid": "placeholder-uuid-from-template",
      "blocks": [
        {
          "type": "paragraph",
          "data": {"text": "This section describes the scope of work."}
        },
        {
          "type": "heading",
          "data": {"text": "Phase 1: Assessment", "level": 2}
        },
        {
          "type": "list",
          "data": {
            "items": [
              "Site inspection and measurements",
              "Material selection and pricing",
              "Timeline development"
            ]
          }
        },
        {
          "type": "table",
          "data": {
            "headers": ["Item", "Qty", "Unit Price", "Total"],
            "rows": [
              ["Copper pipe", "50 ft", "$4.50/ft", "$225.00"],
              ["Labor", "8 hrs", "$85/hr", "$680.00"]
            ]
          }
        }
      ]
    }
  ]
}
```

**When to use content placeholders vs tokens:**
- Tokens: Simple values (names, dates, amounts)
- Content placeholders: Multi-paragraph descriptions, itemized lists, detailed scope of work
- If the content is more than one line, use a content placeholder

## Template Discovery Workflow

### Step 1: List Templates
```
GET /templates?q=service+agreement
```
Returns template IDs and names.

### Step 2: Get Template Details
```
GET /templates/{id}/details
```
Returns tokens, fields, roles, and content_placeholders defined in the template.

### Step 3: Map Aspire Data to Template Fields
Clara maps user intent and extracted parameters to:
1. Tokens — from party data and business context
2. Fields — from structured parameters (dates, amounts, addresses)
3. Content placeholders — from generated scope descriptions

### Step 4: Create Document
```
POST /documents
{
  "name": "Service Agreement - {client_name}",
  "template_uuid": "{template_uuid}",
  "tokens": [...],
  "fields": {...},
  "content_placeholders": [...],
  "recipients": [...]
}
```

## Aspire Template Registry Integration

### Token Mapping from Registry
The Aspire template registry (`template_registry.json`) defines `required_fields` and `required_fields_delta` per template. These map to PandaDoc tokens and fields:

**required_fields (universal):**
- `party_names` → `Client.FirstName`, `Client.LastName`, `Client.Company`, `Sender.Company`
- `template_id` → Internal Aspire reference (not sent to PandaDoc)

**required_fields_delta (template-specific):**
- `scope_description` → Content placeholder (scope_of_work)
- `payment_terms` → Token or field (depends on template design)
- `milestones` → Content placeholder (milestones block)
- `pricing` → Pricing table API
- `property_address` → Token (Custom.PropertyAddress) or field
- `lease_term` → Token (Custom.LeaseTerm)
- `monthly_rent` → Token (Custom.MonthlyRent) or pricing table
- `security_deposit` → Token (Custom.SecurityDeposit)
- `jurisdiction_state` → Token (Custom.JurisdictionState)

### Field Validation Before Submission
Clara validates all required fields are present before calling PandaDoc API:
1. Check `required_fields` from template registry
2. Check `required_fields_delta` from template registry
3. Verify values are non-empty and correctly typed
4. If validation fails, return to user with specific missing field list
5. Never send incomplete documents to PandaDoc (fail closed — Law #3)

## Pricing Tables

### Structure
Pricing tables are separate from tokens/fields/content_placeholders. They populate quote/pricing sections in templates.

**API field:** `pricing_tables` (array)

```json
{
  "pricing_tables": [
    {
      "name": "Pricing Table 1",
      "data_merge": true,
      "options": {
        "currency": "USD",
        "discount": {"type": "absolute", "value": "0"}
      },
      "sections": [
        {
          "title": "Labor",
          "default": true,
          "rows": [
            {
              "options": {
                "optional": false,
                "optional_selected": true,
                "qty_editable": false
              },
              "data": {
                "Name": "Plumbing Installation",
                "Description": "Full bathroom repiping",
                "Price": "2500.00",
                "QTY": "1",
                "Tax": {"type": "percent", "value": "0"}
              }
            }
          ]
        }
      ]
    }
  ]
}
```

### Key pricing table rules:
- `data_merge: true` — Merge with existing pricing table in template
- `data_merge: false` — Replace pricing table entirely
- Column names are case-sensitive: `Name`, `Description`, `Price`, `QTY`
- Price values are strings representing decimal amounts
- Tax can be percentage or absolute amount per row
- Sections group line items (e.g., "Labor", "Materials", "Equipment")

## Common Merge Field Errors

### Token Not Found
If a token name in the API call doesn't match any token in the template, PandaDoc ignores it silently. No error is raised. This can lead to blank fields in generated documents.

**Mitigation:** Always verify token names against template details before creating documents.

### Field API Name Mismatch
Field API names are auto-generated by PandaDoc from field labels. They may contain underscores and lowercase characters even if the label uses spaces and mixed case.

**Mitigation:** Use GET /templates/{id}/details to discover exact field API names.

### Content Placeholder UUID Mismatch
Content placeholder UUIDs change when a template is re-created or modified. Hardcoded UUIDs will fail silently (placeholder region stays empty).

**Mitigation:** Re-fetch template details periodically and update stored UUIDs.

### Empty Token Values
Setting a token value to empty string or null renders nothing in the document. PandaDoc does not raise an error.

**Mitigation:** Clara should validate all required tokens have non-empty values before document creation.
