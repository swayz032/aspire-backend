---
name: Pass 19 Lane C EL Sync Script Patterns
description: EL API schema quirks for sync scripts — webhook object shape, data_collection dict format, workflow node types, process.exit() Windows bug
type: project
---

# Pass 19 Lane C EL Sync Script Patterns (confirmed 2026-05-01 via live probe)

## EL API Schema Quirks

### Conversation Initiation Webhook
- Field: `platform_settings.workspace_overrides.conversation_initiation_client_data_webhook`
- Type: OBJECT not string — `{ url: string, method: "GET"|"POST", request_headers: {} }`
- Required fields: `url`, `method`, `request_headers` (as empty dict `{}`)
- Minimal-merge PATCH: `{ platform_settings: { workspace_overrides: { conversation_initiation_client_data_webhook: { url, method: "GET", request_headers: {} } } } }`
- Idempotency: extract `webhookObj.url` and compare to canonical URL (not the full object)

### Data Collection
- Stored at: `platform_settings.data_collection`
- Type: OBJECT/DICT keyed by field name (NOT an array)
- Schema: `{ [field_name]: { type: "string"|"boolean", description: string } }`
- PATCH: `{ platform_settings: { data_collection: { caller_name: { type: "string", description: "..." }, ... } } }`

### Workflow API
- `workflow.nodes`: object keyed by node_id (NOT structured config like I wrote — EL uses its own internal schema)
- Node type: `"override_agent"` for subagents (NOT `"subagent"`)
- Node structure: `{ type, position: {x, y}, edge_order: [...], conversation_config: {...}, additional_prompt, additional_knowledge_base, ... }`
- `workflow.edges`: OBJECT keyed by edge_id (NOT an array)
- Edge structure: `{ source, target, forward_condition: { label, type: "llm"|"unconditional", condition? }, backward_condition }`
- LLM conditions: natural language string in `forward_condition.condition` (NOT structured variable comparisons)
- Transfer rules (`built_in_tools.transfer_to_number`): at agent level BUT may be stored per-workflow-node or elsewhere; observed as empty `{}` at agent root — check sync-elevenlabs-transfer-rules.mjs for correct path

### Post-Call Webhook
- `GET /v1/convai/settings` returns: `{ webhooks: { post_call_webhook_id: null|string, events, ... }, ... }`
- `post_call_webhook_id` is an EL-internal registry ID — URL is NOT readable via GET API
- Registration: `PATCH /v1/convai/settings` with `{ post_call_webhook: { url: "..." } }` returns 200
- BUT: the webhook ID in `webhooks.post_call_webhook_id` remains null after PATCH — EL stores this differently
- Dashboard verification REQUIRED for post-call webhook

## Windows Node.js Bug
- `process.exit(0)` in mid-async functions on Windows triggers UV handle assertion: `!(handle->flags & UV_HANDLE_CLOSING)`
- Fix: use `return` instead of `process.exit(0)` for idempotent no-op paths. Only use `process.exit(1)` in `.catch()` error handlers (those are safe).

## Transfer Rules State (2026-05-01)
- `built_in_tools` at agent root is `{}` — the 5 dyn-var transfer rules are missing
- This is a PRE-EXISTING state (predates Lane C); Lane C policy: DO NOT modify transfer rules
- The rules may be stored at workflow-node level (check `workflow.nodes.transfer.conversation_config.agent.prompt.built_in_tools`)
- sync-elevenlabs-transfer-rules.mjs is the correct script to restore/manage these (out of Lane C scope)

## Why:
Pass 19 Lane C EL sync scripts — each script hit live EL API and discovered the actual schema diverges significantly from the EL docs. These patterns prevent re-discovery in future sessions.
