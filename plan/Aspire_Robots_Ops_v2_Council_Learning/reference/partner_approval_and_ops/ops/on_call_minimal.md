# Minimal On-call Plan (Solo-Founder Friendly)

- Alert channels: email + SMS (or Slack)
- Severity levels:
  - SEV0: money movement / payroll execution wrong
  - SEV1: persistent provider outage / backlog
  - SEV2: partial degradation
- Immediate mitigation: flip execution_controls to APPROVAL_ONLY or DISABLED
