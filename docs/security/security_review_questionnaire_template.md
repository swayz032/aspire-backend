# Security Review Questionnaire Template

Use this as a single place to answer common partner security review questions (Gusto, Plaid, Stripe, etc.).

## 1. Architecture Overview
- **High-level architecture:** 3-Layer Stack — Intelligence Layer (LangGraph orchestrator on FastAPI :8000) -> Trust Spine (receipts, capability tokens, policy engine, approval flows) -> State Layer (Supabase Postgres + Auth + Realtime, Redis queues).
- **Data stores:** Supabase (Postgres 16 + pgvector), Redis 7 (queues/cache), S3 (blobs/artifacts).
- **Execution boundary:** All side effects flow through the LangGraph orchestrator (Law #1 — Single Brain). No client-side execution. Tools execute bounded commands only (Law #7).
- **API Gateway:** Express Gateway (`backend/gateway/` on :3100) handles auth, rate limiting, schema validation, CORS, and correlation ID propagation.

## 2. Authentication & Authorization
- **Auth provider:** Supabase Auth (JWT-based).
- **Tenant isolation:** Row-Level Security at database layer + tenant-scoped capability tokens (Law #6).
- **Role model:** Suite Owner > Office Admin > Office Member. RLS policies enforce role-based access.
- **Admin privileges:** JWT required in all environments (no dev bypass). Suite ID required for all receipt queries.
- **Session security (MFA, revocation):** Supabase Auth supports MFA. JWT tokens are short-lived with refresh rotation.

## 3. Token & Secret Management
- **Where OAuth tokens are stored:** Supabase (encrypted at rest), scoped to suite_id + office_id + provider.
- **Encryption at rest:** Supabase manages encryption. Railway environment variables for service secrets.
- **Access controls:** Only LangGraph orchestrator and tool executor services can read provider tokens.
- **Rotation policy:** Scheduled (90 days) + emergency rotation. See `docs/security/token_storage_and_rotation.md`.
- **Revocation process:** Per-tenant, per-provider via Admin API or kill switch. Immediate invalidation.
- **Audit logging for privileged access:** All token access generates receipts. Privileged operations logged to audit trail.

## 4. Data Handling & Privacy
- **PII types processed:** Names, email addresses, phone numbers, physical addresses, SSNs (payroll), bank account details (transfers).
- **Retention policy:** Receipts are immutable (never deleted). Provider call logs: 30-90 days. Financial records: per regulatory requirement.
- **Deletion/export process:** Suite owners can request data export. Deletion follows right-to-erasure procedures (non-receipt data only).
- **Log redaction:** DLP via Presidio — SSN, CC, email, phone, address redacted before logging (Gate 5). See `backend/orchestrator/services/dlp.py`.
- **Subprocessors:** Supabase (database), Railway (hosting), Stripe (payments), Gusto (payroll), Plaid (transfers), Moov (money movement).

## 5. Network Security
- **TLS in transit:** All external communication over HTTPS/TLS 1.2+. Railway enforces TLS termination.
- **Inbound IP allowlisting:** Static IP 162.220.234.15 for Domain Rail (whitelisted in ResellerClub).
- **WAF/rate limiting:** Express Gateway rate limiting middleware. Railway-level DDoS protection.

## 6. Webhooks
- **Signature verification:** HMAC-SHA256 verification for all provider webhooks (Gusto, Stripe, Plaid). See `docs/security/webhook_secrets_policy.md`.
- **Replay protection:** Timestamp validation where supported. Idempotency keys prevent duplicate processing.
- **Idempotent processing:** All webhook handlers use idempotency keys. Duplicate events are acknowledged but not re-processed.

## 7. Secure Development Lifecycle
- **Code review:** All changes via GitHub PR. Required review before merge.
- **CI security checks:** Dependency scanning, secret scanning, linting (see `docs/security/vuln_management_checklist.md`).
- **Dependency scanning:** Automated via CI pipeline (pip-audit, pnpm audit).
- **Secrets scanning:** GitHub secret scanning + pre-commit hooks.

## 8. Incident Response
- **On-call owner:** Engineering lead (solo-founder phase), with escalation to external support.
- **Kill switch:** Per-provider, per-tenant execution controls (ENABLED / APPROVAL_ONLY / DISABLED). See `docs/operations/kill_switch.md`.
- **Replay/forensics:** Trace-based replay bundles. Receipt chain provides full audit trail. See `docs/operations/replay_trace.md`.
- **Customer communications:** Status update templates at `docs/operations/status_page_template.md`.

## 9. Business Continuity
- **Backups:** Supabase automated daily backups + point-in-time recovery.
- **RTO/RPO:** RTO: 4 hours (manual failover). RPO: 24 hours (daily backup) / near-zero (point-in-time recovery with WAL).
- **Disaster recovery test cadence:** Quarterly DR test planned (restore from backup, verify receipt chain integrity).
