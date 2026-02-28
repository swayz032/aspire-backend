# Support Playbook: Webhook Delayed

Symptoms:
- provider webhooks not received, state stale

Steps:
1) Check gateway logs + signature failures
2) Check provider status page (manual)
3) Reconcile by polling provider API (read-only) if allowed
4) Emit reconciliation receipts
