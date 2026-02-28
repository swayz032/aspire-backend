# Security Review Questionnaire Template (Fill-In)

Use this as a single place to answer common partner security review questions (Gusto, Plaid, etc.).

## 1. Architecture overview
- **High-level architecture:**
- **Data stores:**
- **Execution boundary:** All side effects via Trust Spine outbox executor (no client-side execution).

## 2. Authentication & authorization
- **Auth provider:**
- **Tenant isolation:** Row-level security + tenant-scoped RPCs.
- **Role model:**
- **Admin privileges:**
- **Session security (MFA, revocation):**

## 3. Token & secret management
- **Where OAuth tokens are stored:**
- **Encryption at rest:**
- **Access controls:**
- **Rotation policy:**
- **Revocation process:**
- **Audit logging for privileged access:**

## 4. Data handling & privacy
- **PII types processed:**
- **Retention policy:**
- **Deletion/export process:**
- **Log redaction:**
- **Subprocessors:**

## 5. Network security
- **TLS in transit:**
- **Inbound IP allowlisting (if any):**
- **WAF/rate limiting:**

## 6. Webhooks
- **Signature verification:**
- **Replay protection:**
- **Idempotent processing:**

## 7. Secure development lifecycle
- **Code review:**
- **CI security checks:**
- **Dependency scanning:**
- **Secrets scanning:**

## 8. Incident response
- **On-call owner:**
- **Kill switch:** per-provider/tenant execution controls.
- **Replay/forensics:** trace-based replay bundles.
- **Customer communications:** status update templates.

## 9. Business continuity
- **Backups:**
- **RTO/RPO:**
- **Disaster recovery test cadence:**
