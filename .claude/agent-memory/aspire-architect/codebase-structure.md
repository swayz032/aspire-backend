---
name: codebase-structure
description: Aspire backend codebase structure — key paths, modules, and test infrastructure.
metadata:
  type: project
---

## Key directories
- `orchestrator/src/aspire_orchestrator/skillpacks/` — main pipeline skillpacks (drew_blueprint.py is 117KB+)
- `orchestrator/src/aspire_orchestrator/services/blueprint/` — blueprint sub-services
- `orchestrator/src/aspire_orchestrator/providers/` — external API clients (azure_doc_intel_client.py etc)
- `orchestrator/src/aspire_orchestrator/config/settings.py` — env var resolution via Settings dataclass
- `orchestrator/tests/` — all tests; fixtures at `orchestrator/tests/fixtures/blueprints/`
- `supabase/migrations/` — sequential SQL migrations (timestamp + description)

## Drew blueprint file size
- `drew_blueprint.py` is ~117KB+ — GitHub MCP push_files fails on it. ALWAYS use git CLI.

## Settings env vars (blueprint)
- `settings.azure_doc_intel_endpoint` = ASPIRE_AZURE_DOC_INTEL_ENDPOINT
- `settings.azure_doc_intel_key` = ASPIRE_AZURE_DOC_INTEL_KEY
- `settings.supabase_url` = ASPIRE_SUPABASE_URL
- `settings.supabase_service_role_key` = ASPIRE_SUPABASE_SERVICE_ROLE_KEY
- `settings.openai_base_url` = optional OpenAI base URL override

## Concurrency env vars
- `ASPIRE_DREW_INGEST_CONCURRENCY` (default: 4) — parallel sheet ingest
- `ASPIRE_DREW_SEE_CONCURRENCY` (default: 4) — parallel YOLO inference + image downloads
- `ASPIRE_DREW_SEE_DISCIPLINES` (default: "A,S,M,E,P,FP,C,L") — wave 9k skip filter

## Migration conventions
- Format: `YYYYMMDDNNNNNN_description.sql` where NNNNNN = sequential 6-digit within same day
- Always include DOWN section as comments at bottom
- Always use `if not exists` / `on conflict do nothing` for idempotency
