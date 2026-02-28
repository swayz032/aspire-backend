# Webhook Secrets Policy

## Storage
- Webhook verification secrets are stored **per provider, per environment** (dev/staging/production).
- Secrets are stored encrypted in Railway environment variables (production) or `.env` files (local dev only).
- Never committed to source control. Never logged (Law #9).

## Providers and Secrets

| Provider | Agent | Secret Location | Signature Method |
|----------|-------|----------------|-----------------|
| Gusto | Milo (Payroll) | `GUSTO_WEBHOOK_SECRET` | HMAC-SHA256 |
| Plaid | Finn (Money Desk) | `PLAID_WEBHOOK_SECRET` | Plaid-Verification header |
| Stripe | Quinn (Invoicing) | `STRIPE_WEBHOOK_SECRET` | Stripe-Signature header (HMAC-SHA256) |
| ResellerClub | Domain Rail | `S2S_HMAC_SECRET` | HMAC-SHA256 (S2S) |

## Verification
- Express Gateway (`backend/gateway/`) verifies webhook signatures before processing.
- Signature verification failure = reject webhook + log security receipt (Law #3 — Fail Closed).
- Replay protection via timestamp validation where supported by provider.

## Rotation Schedule
- **Scheduled**: Rotate every 90 days or per provider recommendation.
- **After incidents**: Immediate rotation if compromise is suspected.
- **Grace period**: If the provider supports multiple active secrets, keep the old secret active for a grace period (typically 24-72 hours) to handle in-flight webhooks during rotation.

## Rotation Procedure
1. Generate new secret in provider dashboard.
2. Update the environment variable in Railway (production) or `.env` (dev).
3. Deploy the service to pick up the new secret.
4. Verify incoming webhooks succeed with the new secret.
5. Revoke the old secret after the grace period expires.
6. Generate a receipt for the rotation event (Law #2).
