# Gateway routes — V1 / V2

> Express routes mounted on the Aspire gateway. All current ElevenLabs-related routes are V1.

| File | Status | Mounted at | Purpose |
|------|--------|------------|---------|
| `elevenlabs-tools.ts` | `[v1]` | `POST /v1/tools/*` | Webhook tool endpoints called by ElevenLabs Conversational AI agents (Ava-EL, Finn-EL, Eli, Nora, Sarah). Endpoints: `/signed-url`, `/context`, `/search`, `/draft`, `/approve`, `/execute`, `/invoke`. |
| `elevenlabs-sessions.ts` | `[v1]` | `POST /v1/sessions/*` | Generates ElevenLabs signed URLs for client sessions. JWT-authenticated. |
| `elevenlabs-webhooks.ts` | `[v1]` | `POST /v1/webhooks/elevenlabs/*` | Post-call transcript ingestion (HMAC-verified) + Sarah's conversation-init webhook. |

## Auth

All `/v1/tools/*` requests are protected by `../middleware/elevenlabs-auth.ts` `[v1]`, which validates the `x-elevenlabs-secret` header against `ELEVENLABS_TOOL_SECRET` env. Tenant `suite_id` must be a valid UUID — fail-closed otherwise.

## V2 routes

V2 routes (Anam → orchestrator → LangGraph) currently flow through the Aspire-desktop server (`Aspire-desktop/server/agentToolRoutes.ts`) and the orchestrator HTTP server directly (`backend/orchestrator/src/aspire_orchestrator/server.py:946`, `POST /v1/intents`). The gateway does not yet host V2 routes.
