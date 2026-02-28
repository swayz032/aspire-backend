# Minimal SLA & Support Policy (Starter)

This is intentionally simple for a solo founder. Adjust once you have staffing.

## Support channels
- In-app support ticket
- Email support

## Response targets (business days)
- **Critical (P0):** acknowledge within 1 hour
- **High (P1):** acknowledge within 4 hours
- **Normal (P2):** acknowledge within 1 business day
- **Low (P3):** acknowledge within 3 business days

## Definitions
- **P0:** money/payroll actions stuck or executing incorrectly, or security incident.
- **P1:** major workflow failure for many users; degradation without data loss.
- **P2:** single-tenant workflow issue with workaround.
- **P3:** questions, cosmetic bugs.

## Mitigation controls
- Flip provider/tenant to `APPROVAL_ONLY` or `DISABLED` via execution controls.
- Pause new onboarding to affected provider.
- Use replay bundle to isolate root cause.
