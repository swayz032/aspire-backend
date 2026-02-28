# Reflect Governance Policy

## What reflect is
A controlled mechanism that proposes updates to Markdown skill files based on:
- Corrections
- Approvals
- Repeated mistakes
- Successful patterns

## What reflect is NOT
- An autonomous authority to change core invariants
- A substitute for review

## Risk tiers
### Low-risk (auto-propose OK)
- Formatting and readability conventions
- Non-security naming consistency
- Minor UX patterns (loading/error/empty state)

### Medium-risk (manual review required)
- Logging fields
- Error handling conventions
- Receipt formatting details

### High-risk (human approval required)
- Any change to SAFETY rules
- Any change to receipt invariants
- Anything that could weaken security, privacy, tenancy, or auditability

## Output requirements (every run)
- Proposed edits with confidence: high/medium/low
- Diff
- Rationale + evidence quote snippets
- Proposed commit message

## Storage
- Proposals are written to `proposed/` with timestamped folders.
- Never overwrite existing proposals.

## Review rule
- No merge without a human approving the diff.
