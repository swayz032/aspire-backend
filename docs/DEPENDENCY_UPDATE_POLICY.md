# Dependency Update Policy

This repo uses a staged dependency promotion model across four domains:

- `backend`: `backend/orchestrator` and `backend/gateway`
- `admin_portal`: `Aspire/Aspire`
- `desktop`: `Aspire-desktop`
- `shared_contracts`: backend schemas and Ava contract files consumed across surfaces

## Defaults

- Update pace: `minor lag`
- Automation: `auto PR + gate`
- Production deploys only from reviewed lockfiles
- Backend framework upgrades are never auto-merged

## Protected backend framework packages

These packages require explicit framework review:

- `langgraph*`
- `langchain-*`
- `fastapi`
- `pydantic*`
- `openai`
- `psycopg*`

If a PR changes one of these packages, the `Dependency Update Gate` workflow requires either:

- `framework-migration`
- `framework-review`

## Gate behavior

- Backend dependency PRs run orchestrator certification slices plus gateway build/test
- Admin portal dependency PRs run admin build/lint
- Desktop dependency PRs run desktop build/lint
- Shared contract changes run backend + admin portal + desktop gates

## Source of truth

Automation reads the repo policy from:

- [`.github/dependency-policy.yml`](/C:/Users/tonio/Projects/myapp/.github/dependency-policy.yml)
- [`.github/dependabot.yml`](/C:/Users/tonio/Projects/myapp/.github/dependabot.yml)
- [`.github/workflows/dependency-update-gate.yml`](/C:/Users/tonio/Projects/myapp/.github/workflows/dependency-update-gate.yml)
