# Aspire Global Style Skill (STYLE.md)

## Purpose
This skill defines **project-wide** style and consistency rules for all code, specs, and operational artifacts.

## Non-negotiables
- Prefer clarity over cleverness.
- No silent behavior changes: explain intent in the smallest number of words necessary.
- All externally visible identifiers must be stable and documented.

## Naming
- Use `kebab-case` for filenames, `PascalCase` for React components, `camelCase` for variables/functions.
- Prefer domain terms from Aspire: **receipt**, **incident**, **run**, **correlationId**, **tenantId**.

## Logging (Style Only)
- Logs must be structured (JSON) and include: `timestamp`, `level`, `service`, `correlationId`, `tenantId` (if applicable), `event`, `message`.
- Avoid logging secrets/PII.

## UI / React (Style Only)
- Keep components small and composable.
- Validate user input at the boundary (forms/handlers).
- Prefer explicit loading/error/empty states.

## Update Policy
Edits to this file must:
1. Include rationale.
2. Avoid contradicting other skills.
3. Be reviewed via a diff and committed to Git.

## Changelog
- (append entries here)
