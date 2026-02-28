# Meeting of Minds (Ops runbook)

## Goal
Use multiple models as **advisors**, not executors.

## Flow
1) Robots failure -> incident.opened receipt + A2A triage message
2) Ava spawns council:
   - GPT (critic), Gemini (critic), Security reviewer (optional)
3) Each advisor outputs structured triage proposal:
   - root cause, fix plan, tests, risk tier, evidence links
4) Ava adjudicates -> triage.decision receipt
5) Claude builds patch -> patch receipts
6) Robots retest -> patch.verified receipt
7) Ava requests release approval -> canary deploy via outbox


## Council members (v2)
- OpenAI (GPT) — architecture
- Gemini — research cross-check
- Claude agent — implementation plan

## Evidence Pack
Council uses read-only evidence packs; no tool execution.
