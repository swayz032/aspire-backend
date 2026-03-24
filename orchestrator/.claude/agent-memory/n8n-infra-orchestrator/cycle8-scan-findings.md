---
name: cycle8-scan-findings
description: Cycle 8 production scan results for n8n workflows 17-24 — key patterns found including kill switch data loss, cross-tenant queries, unprotected webhooks
type: project
---

## Cycle 8 Scan (2026-03-23) — Workflows 17-24

**18 findings**: 2 HIGH, 9 MEDIUM, 7 LOW

### Critical Patterns Discovered

1. **Kill Switch data loss (Approval Change Handler)**: Kill Switch node returns only `{ killed: 'false' }` without spreading incoming webhook data. Downstream HMAC validation receives empty object, always fails. Pattern: EVERY kill switch node must spread `$input.first().json`.

2. **Non-standard governance-gate receipts**: The `Receipt: FAILED (0)` nodes added by the production hardening pass use a flat schema (`receipt_type`, `action_type`, `tenant_id`, `details`, `trace_id`) instead of standard receipt schema (`receipt_id`, `suite_id`, `office_id`, `actor_type`, `actor_id`, `action: {}`, `correlation_id`, `created_at`). Affects 5 workflows.

3. **Cross-tenant inbox query (Batch Email Digest)**: Fetches `inbox_items` with service_role_key and NO suite_id filter — reads all tenants. Law #6 violation.

4. **Unprotected manual webhook triggers**: Adam Pulse Scan and Adam Library Curate have secondary webhook triggers with no HMAC validation.

5. **trace_id propagation gap (Intake Activation)**: Generate Trace ID node output is consumed by Kill Switch, but Kill Switch re-reads raw $input and doesn't carry trace_id forward. Downstream receipt x-trace-id header references undefined.

**Why:** These patterns indicate systemic issues from the original hardening pass that injected governance nodes without fully integrating them into existing data flows.

**How to apply:** When scanning workflows, always trace the data flow from trigger through kill switch to verify payload propagation. Check that kill switch nodes spread incoming data. Check that governance-gate receipts use standard schema.
