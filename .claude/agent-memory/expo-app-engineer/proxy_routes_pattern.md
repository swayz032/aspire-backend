---
name: Aspire Desktop server proxy route pattern
description: How to mint capability tokens and forward authenticated requests from Express to the Python orchestrator
type: reference
---

# Aspire Desktop server proxy pattern

The Aspire-desktop Express server (`server/routes.ts`) acts as a proxy between
the React frontend and the Python orchestrator. Frontend calls same-origin
`/api/v1/...` paths; the Express layer:

1. Validates the JWT (`requireAuth(req, res)` returns suite_id derived from JWT)
2. Mints a short-lived capability token (45s, max 59s) via
   `server/lib/capabilityToken.ts` for write-tier scopes
3. Forwards to `${ORCHESTRATOR_URL}/v1/...` with these headers:
   - X-Tenant-Id (= suite_id in single-tenant-per-suite)
   - X-Suite-Id
   - X-Office-Id
   - X-Actor-Id
   - X-Correlation-Id
   - X-Trace-Id
4. Forwards orchestrator response status + JSON envelope verbatim

## Capability token canonical format

Must match Python orchestrator's `validate_token()` byte-for-byte:
- HMAC-SHA256(canonical_json, TOKEN_SIGNING_SECRET)
- Canonical = `JSON.stringify(sortedKeyObject)` with these required fields:
  `token_id, suite_id, office_id, tool, scopes (sorted!), issued_at,
   expires_at, correlation_id`
- Full token = signed payload + `signature` (hex) + `revoked: false`

## Key files
- `server/lib/capabilityToken.ts` — mintCapabilityToken helper
- `server/routes.ts` — `proxyForward()` shared forwarder + 12 `/api/v1/...`
  proxy routes (search-memory, front-desk × 6, twilio × 3, sms × 1, memory-detail)
- Reference: `enrich-product` route in routes.ts shows the original mint
  pattern this is generalized from

## Frontend API clients
- `lib/api/officeMemory.ts` — search/detail
- `lib/api/frontDesk.ts` — config CRUD + Twilio search/purchase
- `lib/api/sms.ts` — sms send
- All take `authenticatedFetch` from `useAuthFetch()` + `officeId` from `useTenant()`

## 401 retry-once
`lib/authenticatedFetch.ts` retries the request once after `supabase.auth.refreshSession()`
on 401. Skips retry on authoritative codes: INVALID_SIGNATURE, SCOPE_MISMATCH,
TENANT_ISOLATION_VIOLATION, CAPABILITY_DENIED.
