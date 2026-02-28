# Key Rotation Runbook

## Keys That Require Rotation

| Key Type | Location | Rotation Schedule | Agent Impact |
|----------|----------|------------------|-------------|
| Webhook secrets (Gusto, Stripe, Plaid) | Railway env vars | Every 90 days + after incidents | Milo, Quinn, Finn |
| OAuth client secrets | Provider dashboards + Railway env vars | Per provider policy + after incidents | All provider-connected agents |
| Receipt signing keys | Railway env vars (`RECEIPT_SIGNING_KEY`) | Kid-based rotation, no fixed schedule | All (Trust Spine) |
| S2S HMAC secret (Domain Rail) | Railway env vars (`S2S_HMAC_SECRET`) | Every 90 days + after incidents | Domain Rail service |
| JWT signing key | Supabase Auth config | Per Supabase policy | All (authentication) |
| Metrics auth token | Railway env vars (`ASPIRE_METRICS_TOKEN`) | Every 90 days | Ops (Prometheus/Grafana) |

## Webhook Secret Rotation

1. Generate new secret in the provider dashboard (Gusto/Stripe/Plaid).
2. If the provider supports multiple active secrets, add the new secret alongside the old one.
3. Update the environment variable in Railway (`GUSTO_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_SECRET`, `PLAID_WEBHOOK_SECRET`).
4. Deploy the affected service to pick up the new secret.
5. Verify incoming webhooks succeed with the new secret (check Express Gateway logs).
6. After the grace period (24-72 hours), remove the old secret from the provider dashboard.
7. Generate a receipt for the rotation event (Law #2).

## OAuth Client Secret Rotation

1. Generate new client secret in the provider developer dashboard.
2. Update the environment variable in Railway.
3. Deploy the affected service.
4. Verify OAuth token refresh succeeds with the new client secret.
5. Revoke the old client secret in the provider dashboard.
6. Generate a receipt for the rotation event.

## Receipt Signing Key Rotation (Kid-Based)

1. Generate a new signing key and assign a new `kid` (key ID).
2. Add the new key to the signing key configuration (the old key remains for verification).
3. Deploy the orchestrator service.
4. New receipts are signed with the new `kid`.
5. Old receipts remain verifiable using the old key (identified by their `kid`).
6. The receipt chain verifier (`backend/orchestrator/services/receipt_chain.py`) supports multi-key validation.
7. Old signing keys are **never deleted** — they are retained read-only for historical receipt verification.

## Emergency Rotation
- Triggered by: suspected compromise, security incident, partner notification.
- Procedure: Same as scheduled rotation, but with `execution_controls` set to `APPROVAL_ONLY` during the rotation window.
- See: `docs/operations/incident_response.md` for full incident procedure.
