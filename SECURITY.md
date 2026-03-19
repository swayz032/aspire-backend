# Security Policy

## Reporting a Vulnerability

1. **Do NOT** open a public GitHub issue
2. Email: security@aspireos.app
3. Include: description, reproduction steps, impact assessment
4. Expected response: within 48 hours

## Security Practices

- Governance pipeline: Intent → Policy → Approval → Execute → Receipt
- Row-Level Security (RLS) enforces tenant isolation at DB layer
- Capability tokens: short-lived (<60s), scoped, server-verified
- PII redacted via Presidio DLP
- Secrets managed via AWS Secrets Manager with automated rotation
