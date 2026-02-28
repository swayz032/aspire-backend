# Control Person Checklist — Plaid Transfer (Private Company)

## Required Information

Prepare the following for the Plaid Transfer application:

### Control Person Details
- **Name**: [Full legal name of control person]
- **Title**: [Officer/Director title within the company]
- **Identification details**: As required by Plaid (SSN last 4, date of birth, address)
- **Evidence of authority**: Board resolution, articles of incorporation, or officer appointment documentation (if requested by Plaid)
- **Internal point of contact**: Engineering lead or CTO for verification requests

### Company Details
- **Legal entity name**: Aspire Operations Inc. (or operating entity)
- **EIN/Tax ID**: [To be provided securely — never stored in source control]
- **Business address**: [Registered business address]
- **State of incorporation**: [State]
- **Business type**: Technology platform (SaaS) for small business execution infrastructure

### Aspire-Specific Context
- Aspire operates as a **Platform** facilitating ACH transfers for small business customers.
- Each customer (suite) is a separate originator with isolated funds flow (Law #6 — Tenant Isolation).
- Aspire does not hold, pool, or commingle customer funds.
- All transfer operations are governed by the 7 Immutable Laws and require RED-tier approval.

## Security Notes
- Never store control person PII in source control or documentation files.
- Use secure channels (encrypted email, secure document sharing) for transmitting identification details.
- PII submitted to Plaid is redacted from all Aspire logs via DLP/Presidio (Gate 5).
