---
name: Runbook Style Guide
description: How to match office-memory-engine.md style and depth when writing new Aspire runbooks
type: feedback
---

Match the style of `Aspire-desktop/docs/runbooks/office-memory-engine.md`:

- Frontmatter: subsystem, version, pass, authored, owner
- ASCII architecture diagram in a code block — shows data flow end-to-end
- Sections in order: What This Runbook Covers → Architecture → Common Procedures → Failure Modes + Recovery → Rollback → Monitoring
- Every procedure has copy-paste `railway run` Python REPL commands + verification SQL
- Failure modes use bold headings, Symptom/Cause/Diagnosis/Recovery structure
- Rollback section is DESTRUCTIVE and clearly labeled
- Cross-link to related runbooks at the top of the file
- Monitoring section references Prometheus metric names (even if not yet implemented — note the pass when they land)
- No fluff: write for an engineer at 3am during an incident

**Why:** Runbooks that don't match this style are harder to scan under pressure. Consistency across telephony.md, sarah-personalization.md, sms.md, office-memory-engine.md is a deliberate operational standard.
**How to apply:** For any new runbook in docs/runbooks/, use office-memory-engine.md as the structural template before writing content.
