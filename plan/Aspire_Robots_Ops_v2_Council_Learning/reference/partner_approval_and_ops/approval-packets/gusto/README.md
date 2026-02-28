# Gusto Approval Packet (Template)

Goal: package the *minimum reviewer evidence* to speed up production access.

## What to include (attachments)
1. **Architecture diagram** (Aspire → Gateway → Trust Spine → Gusto) with token storage + tenant boundaries.
2. **End-to-end demo script** + screenshots (or short recording).
3. **Strict access compliance proof**
   - evidence: token audit export showing company-scoped grants
   - link: `../../program/gusto/01_strict_access_checklist.md`
4. **Scopes mapping** (principle of least privilege)
   - link: `../../program/gusto/02_scopes_mapping.md`
5. **Webhook mapping + verification**
   - link: `../../program/gusto/05_webhook_mapping.md`
   - link: `../../gateway/webhooks/SIGNATURE_VERIFICATION_CHECKLIST.md`
6. **Security review questionnaire answers** (draft)
   - link: `../../security/security_review_questionnaire_template.md`
7. **Support readiness**
   - playbooks: `../../ops/support_playbooks/*`
8. **Incident readiness**
   - kill switch + replay runbooks: `../../runbooks/*`
9. **Evidence exports**
   - receipts, provider_call_log: `../../evidence/*`

## Assemble checklist
Use `../submission_checklist.md` and mark every item as complete.
