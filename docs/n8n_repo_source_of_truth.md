# n8n Repo Source of Truth

This project enforces **repo -> live n8n** as the authority model.

## Required secrets
- `N8N_API_URL`
- `N8N_API_KEY`

## Drift gate
- CI workflow: `.github/workflows/n8n-drift-gate.yml`
- Script: `scripts/check_n8n_drift.py`

The gate fails if any mapped workflow in live n8n differs from the JSON under:
- `infrastructure/n8n-workflows/` (agent workflows)
- `infrastructure/n8n/` (ops workflows)

## Operational flow
1. Update workflow JSON in repo.
2. Sync workflows to n8n using your existing sync script/tooling.
3. Run drift check:
   - `python scripts/check_n8n_drift.py`
4. Merge only when drift check passes.

## Notes
- If `N8N_API_URL` or `N8N_API_KEY` are missing, drift check fails closed.
- Keep workflow IDs in sync with script mapping.

