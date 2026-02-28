# Redaction / DLP Matrix (Telemetry Facade Boundary)

**Source:** Ava Admin Enterprise Handoff v2
**Goal:** ensure Admin Portal + Ava never receive secrets or unsafe payloads, while still enabling operational diagnosis.

## Redaction classes
| Class | Examples | Handling |
|---|---|---|
| Secrets | API keys, OAuth codes, refresh tokens, bearer tokens, webhook signing secrets, private keys | Replace with `[REDACTED:SECRET]` and drop full value from logs/telemetry |
| Credentials | passwords, OTPs, magic links | `[REDACTED:CREDENTIAL]` |
| Financial identifiers | bank acct/routing, card PAN, SSN/TIN, full invoice PDFs | Mask (last4 only) or replace with `[REDACTED:FINANCIAL]` |
| PII | email, phone, address, DOB | Mask by policy; operator mode shows minimal; engineer mode shows hashed or masked values |
| Untrusted text | provider payload fields, email bodies, webhook payloads, user-entered notes | Always treat as inert text; escape/quote; never use as tool arguments |
| Internal infra secrets | database URLs, connection strings, service tokens | `[REDACTED:INFRA]` |

## Deterministic redaction rules (minimum)
Telemetry Facade must apply these before any response serialization:
- Header keys: `authorization`, `x-api-key`, `cookie`, `set-cookie` -> drop or redact
- Common patterns (case-insensitive):
  - `sk-[A-Za-z0-9]{20,}` -> `[REDACTED:SECRET]`
  - `Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*` -> `[REDACTED:SECRET]`
  - `-----BEGIN (?:RSA|EC|PRIVATE) KEY-----` blocks -> `[REDACTED:KEY]`
- JSON keys to redact recursively:
  - `token`, `refresh_token`, `access_token`, `client_secret`, `secret`, `api_key`, `password`, `signature`, `webhook_secret`

## Prompt-injection safety rules
- Any field derived from external systems (providers, emails, webhooks, logs) is untrusted.
- Render untrusted text as:
  - code block or quoted text
  - never concatenated into prompts that can affect tool selection or policy decisions
- Policy engine decisions must use structured fields, not freeform strings.

## Auditability
- Redaction must be logged as counters/metrics, not as the raw pre-redacted values.
- Receipt payloads must store:
  - `redaction_applied: true`
  - `redaction_ruleset_version`
  - counts by class (secret/pii/financial/untrusted/infra)

## Cross-reference
- CLAUDE.md Law #9 (Security & Privacy Baselines) — PII redaction rules
- Implementation target: Phase 2 (Gate 05: Security)
