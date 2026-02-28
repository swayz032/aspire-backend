# Status Page Template

## Components

| Component | Description | Health Check |
|-----------|------------|-------------|
| Ava Orchestrator | LangGraph decision engine (FastAPI :8000) | `/healthz`, `/readyz` |
| Express Gateway | API gateway, webhook ingress (:3100) | `/healthz` |
| Trust Spine | Receipts, capability tokens, policy engine | Receipt chain integrity check |
| Provider: Gusto | Payroll operations (Milo agent) | Provider status page + webhook latency |
| Provider: Plaid Transfer | Money movement (Finn agent) | Provider status page + webhook latency |
| Provider: Stripe | Invoicing + payments (Quinn agent) | Provider status page + webhook latency |
| Domain Rail | Domain management service | `/healthz` on Railway |
| Notifications/Webhooks | Outbound notifications + inbound webhook processing | Express Gateway metrics |

## Incident Update Format

### Title
[Component] — [Brief description of impact]

### Body
- **Summary**: What is happening and who is affected.
- **Start time (UTC)**: When the issue was first detected.
- **Impacted components**: Which components from the table above are affected.
- **Customer impact**: What users are experiencing (specific skill packs, operations blocked, degraded performance).
- **Mitigation steps**: What has been done so far (include kill switch usage if applicable).
- **Recovery steps**: What is being done to resolve the issue.
- **Next update time**: When the next status update will be posted.

## Postmortem Template

### Title
[Date] — [Incident summary]

### Sections
- **Root cause**: Technical explanation of what went wrong.
- **Timeline**: Chronological sequence of events (detection, mitigation, resolution).
  - Include: when alerts fired, when kill switch was activated, when fix was deployed.
- **What went well**: Detection, response, communication, tooling that worked.
- **What went wrong**: Gaps in monitoring, slow response, missing playbooks.
- **Action items**: Specific improvements with owners + due dates.
  - Each action item should reference the relevant Production Gate (Gate 1-5).
  - Include: new tests, monitoring improvements, playbook updates, infrastructure changes.
