# Claude Handoff Add-on: Partner Approval + Ops (10/10)

These items are intentionally **outside** Trust Spine, but should be implemented to the same standard:
contracts, invariants, idempotency, replay, and evidence exports.

## Systems covered
- Gateway webhook verification + idempotent ingestion
- Provider adapter contract enforcement
- Program docs (funds flow, KYB/KYC, returns handling)
- Security posture artifacts (token handling, strict access)
- Ops readiness (support playbooks, incident response)
- Approval packets (Gusto, Plaid Transfer)

## Invariants
1) No side effects from UI or "brain" directly — only outbox executor.
2) Every inbound webhook is signature-verified and idempotent.
3) Every outbound provider call is idempotent and yields a receipt.
4) High-risk actions require approval + capability token (Trust Spine) — gateway/enforcer must enforce.

## v2 additions
- Approval packet templates in `approval-packets/` with a generic submission checklist.
- Evidence pack generator: `evidence/generate_evidence_pack.sh`.
- Security questionnaire template: `security/security_review_questionnaire_template.md`.
- Status + SLA templates: `ops/templates/`.
