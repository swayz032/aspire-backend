# Skill Packs — V1 / V2 / Discontinued

> The canonical home for ALL active Aspire skill packs.
> Pair with `myapp/docs/Aspire-System-Map-v2.md`.

| File | Status | Used by | Purpose |
|------|--------|---------|---------|
| `base_skill_pack.py` | `[shared]` | All packs | Base class. |
| `ava_user.py` | `[v1-active]` | Registered as `"ava"` in `nodes/agent_dispatch.py:60` | Ava channel/user-facing skill pack. |
| `ava_admin.py` | `[v1-active]` | Registered as `"ava_admin"` in `nodes/agent_dispatch.py:61` | Ava control-plane wrapper (yellow tier). |
| `ava_admin_desk.py` | `[v1-active]` | Imported by `ava_admin.py:6` and `routes/admin.py` | Backend implementation for admin ops. |
| `eli_inbox.py` | `[v1-active]` | ElevenLabs Eli + LangGraph dispatch | Email triage, drafts. |
| `finn_finance_manager.py` | `[v1-active]` | ElevenLabs Finn-EL + Anam Finn-Anam | Finance hub manager. |
| `nora_conference.py` | `[v1-active]` | ElevenLabs Nora | Video conference orchestration. |
| `sarah_front_desk.py` | `[v1-active]` | ElevenLabs Sarah + Twilio | Front desk telephony. |
| `quinn_invoicing.py` | `[v1-active]` | Ava-EL `invoke_quinn` via `/v1/agents/invoke-sync` (BYPASS) | Stripe invoicing. |
| `adam_research.py` | `[v1-active]` | Ava-EL `invoke_adam` via `/v1/agents/invoke-sync` (BYPASS) | Research / vendor lookup. |
| `tec_documents.py` | `[v1-active]` | Ava-EL `invoke_tec` via `/v1/agents/invoke-sync` (BYPASS) | Document generation. |
| `clara_legal.py` | `[v1-active]` | Ava-EL `invoke_clara` (when wired) via `/v1/intents` | Legal / contracts (PandaDoc). |
| `mail_ops_desk.py` | `[v1-active]` | Sarah / Ava-EL routing via `/v1/intents` | Domain & mailbox management. |
| `qa_evals.py` | `[shared]` | Internal ops | QA / evals harness. |
| `release_manager.py` | `[shared]` | Internal ops | Release ops. |
| `security_review.py` | `[shared]` | Internal ops | Security review. |
| `sre_triage.py` | `[shared]` | Internal ops | SRE / triage. |
| `teressa_books.py` | `[discontinued]` | (was Teressa — QuickBooks) | **DISCONTINUED.** Inventoried in System Map Appendix A. Do not extend. |
| `milo_payroll.py` | `[discontinued]` | (was Milo — Gusto) | **DISCONTINUED.** Inventoried in System Map Appendix A. Do not extend. |
| `__init__.py` | `[shared]` | Package marker | |

## Migration debt

- `quinn_invoicing.py`, `adam_research.py`, `tec_documents.py` are reachable via the `/v1/agents/invoke-sync` bypass endpoint. They run inline without policy gate, token mint, or central receipt. Migrating these to be invoked from inside LangGraph nodes (instead of as a separate sync endpoint) is a Yellow-tier follow-up plan.
