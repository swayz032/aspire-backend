# backend/ — V1 vs V2 Quick Map

> Pair with `myapp/docs/Aspire-System-Map-v2.md` (full map) and `myapp/.claude/workspace.json` (registry).

**The axis:** V1 = ElevenLabs/Anam agents whose own LLM is the brain. V2 = Anam personas where **LangGraph IS the brain**. ElevenLabs voice agents (Eli, Nora, Sarah, Ava-EL, Finn-EL) are V1. Anam personas (Ava-Anam, Finn-Anam) are V2.

## Top-level directories

| Directory | Status | Purpose |
|-----------|--------|---------|
| `orchestrator/` | `[v2]` | LangGraph 14-node graph + Temporal workflows. The Single Brain (Law #1). |
| `gateway/` | `[v1]` | Express gateway hosting ElevenLabs tool/webhook routes. |
| `safety-gateway/` | `[shared]` | NeMo Guardrails sidecar. Decoupled service. |
| `platform/` | `[shared]` | Live runtime policy configs (Finn allowlists, n8n MCP specs). |
| `infrastructure/` | `[shared]` | Railway / AWS / Docker deploy configs. |
| `scripts/` | `[mixed]` | `setup-anam-personas.ts` is `[v2]`; rest are `[shared]`. |
| `supabase/` | `[shared]` | Canonical platform DB migrations (project: `myapp`). |
| `tests/` | `[shared]` | Cross-cutting integration / e2e tests. |
| `tools/` | `[shared]` | Dev automation (CI validators, git hooks, CLI). |
| `docs/` | `[shared]` | Backend documentation. |

## Skill packs (V1-active, used by V1 frontstage)

Real location: `backend/orchestrator/src/aspire_orchestrator/skillpacks/`

- **Active V1-backstage:** `quinn_invoicing.py`, `adam_research.py`, `tec_documents.py`, `clara_legal.py`, `mail_ops_desk.py`
- **Active frontstage doubles:** `ava_user.py`, `ava_admin.py`, `ava_admin_desk.py`, `eli_inbox.py`, `sarah_front_desk.py`, `finn_finance_manager.py`, `nora_conference.py`
- **Internal/ops:** `qa_evals.py`, `release_manager.py`, `security_review.py`, `sre_triage.py`, `base_skill_pack.py`
- **Discontinued (inventory only):** `teressa_books.py`, `milo_payroll.py`

See `backend/orchestrator/src/aspire_orchestrator/skillpacks/_V1_V2_INDEX.md` for the per-file table.

## V1 entry points

- `gateway/src/routes/elevenlabs-tools.ts` — webhook tool endpoints
- `gateway/src/routes/elevenlabs-sessions.ts` — signed-URL session generation
- `gateway/src/routes/elevenlabs-webhooks.ts` — transcripts + Sarah conversation-init
- `gateway/src/middleware/elevenlabs-auth.ts` — `x-elevenlabs-secret` validation
- `orchestrator/src/aspire_orchestrator/server.py:1467-1472` — `/v1/agents/invoke-sync` (BYPASSES LangGraph for Quinn/Adam/Tec)

## V2 entry points

- `orchestrator/src/aspire_orchestrator/server.py:946` — `invoke_orchestrator_graph()` reached via `POST /v1/intents`
- `orchestrator/src/aspire_orchestrator/graph.py` — LangGraph definition
- `orchestrator/src/aspire_orchestrator/temporal/workflows/ava_intent.py` — durable outer wrap
- `scripts/setup-anam-personas.ts` — V2 Anam persona setup

## Recently archived (2026-04-28)

- `_archive/2026-04-28/backend/skillpacks/` — old TypeScript finn-finance-manager prototype (zero external references)
- `_archive/2026-04-28/backend/backend/` — empty nested stub directory

## Open follow-ups (out of scope here)

- Quinn/Adam/Tec live on `/v1/agents/invoke-sync` (bypass). Migrate to `/v1/intents` so LangGraph governs.
- Teressa (`teressa_books.py`) and Milo (`milo_payroll.py`) are discontinued; files inventoried in System Map Appendix A; removal in a follow-up plan.
- `tax_rules/` (root) and `orchestrator/.../config/tax_rules/` differ; manual reconciliation needed before archiving the root copy.
