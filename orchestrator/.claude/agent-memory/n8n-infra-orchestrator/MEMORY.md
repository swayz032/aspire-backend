# n8n Infrastructure Orchestrator Memory

## Scan History
- [Cycle 8 Findings](cycle8-scan-findings.md) — 2026-03-23, workflows 17-24, 18 findings (kill switch data loss, cross-tenant query, unprotected webhooks, non-standard receipts)

## Key Patterns
- Kill switch nodes MUST spread `$input.first().json` — returning only `{ killed }` drops webhook payload and breaks downstream HMAC
- Production hardening pass inserted `Receipt: FAILED (0)` nodes with non-standard flat schema — need alignment across all workflows
- Manual webhook triggers on scheduled workflows lack HMAC validation
- Supabase direct queries with service_role_key need explicit suite_id filters to maintain tenant isolation
