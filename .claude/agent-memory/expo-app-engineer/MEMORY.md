# Expo App Engineer — Aspire Desktop Memory

## Architecture
- [Server proxy route pattern](proxy_routes_pattern.md) — How `/api/v1/...` proxies mint capability tokens and forward to orchestrator. Use `proxyForward()` helper in `routes.ts` for any new same-origin proxy.
