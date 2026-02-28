# ACH Returns SOP

Steps:
1) Verify webhook signature; ingest idempotently
2) Record return receipt with stable taxonomy code
3) Notify customer; provide next steps
4) Apply policy: reduce limits / approval-only / disable
5) Escalate high-value or repeated returns
