# Runbook: Policy Misconfiguration

**Source:** Ava User Enterprise Handoff v1.1

## Symptom
Unexpected denies or unexpected allows.

## Steps
1. Compare policy version in `policy_decision` receipts.
2. Validate allowlist intersection logic.
3. Run negative tests (bypass attempts).
4. Roll back policy to last known-good version.
