# Unwrangle Provider

**Base URL:** `https://data.unwrangle.com`
**Auth:** Query parameter `api_key` (no headers)
**Risk tier:** GREEN (read-only)
**Settings key:** `unwrangle_api_key` / env `ASPIRE_UNWRANGLE_API_KEY`

## Verified Endpoints (as of 2026-05-17)

| Platform | Client Function | Status | Notes |
|----------|----------------|--------|-------|
| `lowes_store` | `find_nearest_store` | WORKING | 10 GA stores for zip 30297 |
| `lowes_category` | `browse_category` | WORKING | 431 dishwasher results with store inventory |
| `lowes_detail` | `get_product_detail` | WORKING | Full Husqvarna spec with variants |
| `lowes_search` | `search_products` | BROKEN (504) | See below |
| `homedepot_search` | `search_products` (HD client) | WORKING | 2569 PVC pipe results |
| `build_search` | Ferguson `search_products` | WORKING | Ferguson/Build.com products |
| `build_detail` | Ferguson `get_product_detail` | WORKING | Full Ferguson product detail |

## 504 Skip Path — lowes_search

Unwrangle's `lowes_search` platform returns HTTP 504 from their upstream scraper.
This is a **known, confirmed upstream failure** (Unwrangle support ticket filed, Wave 5.2 re-enable candidate).

**Behaviour implemented (commit 5.4c):**

- `search_products()` is wired in `unwrangle_lowes_client.py` for forward compatibility.
- On 504: emits receipt with `reason_code=UPSTREAM_DEGRADED` and `upstream_skip=provider.unwrangle_lowes.504_skip`.
- Returns `data={"results": [], "result_count": 0, "upstream_error": 504}` so caller can inspect.
- **DO NOT retry on 504** — Unwrangle has confirmed the upstream will not recover within a request cycle. Retrying wastes credits.
- The `adam_supplier_router._call_unwrangle_lowes_search()` wrapper returns `([], 0)` on failure — zero credits charged to the route budget.

**Re-enable checklist (Wave 5.2):**
1. Verify Unwrangle ticket resolved (test manually with `ASPIRE_UNWRANGLE_API_KEY` set).
2. Run `TestUnwrangleLowesSearchProductsIntegration` — it should now get results, not a 504.
3. Update `search_products` docstring to remove the "known broken" note.
4. Remove the `upstream_skip` field logic and update tests to assert happy-path.

## Error Codes

| HTTP | Meaning | Internal Code |
|------|---------|--------------|
| 401 | Invalid API key | `AUTH_INVALID_KEY` |
| 402 | Credits exhausted | `RATE_QUOTA_EXCEEDED` |
| 429 | Rate limited | `RATE_LIMITED` |
| 504 | Upstream scraper down | `UPSTREAM_DEGRADED` (reason: `UPSTREAM_DEGRADED`) |
| 500-503 | Unwrangle server error | `SERVER_UNAVAILABLE` |
| 200 `success=false` + "unable to fetch" | Upstream degraded soft signal | `UPSTREAM_DEGRADED` |

## Idempotency

All Unwrangle endpoints are read-only GET requests. No idempotency keys required.
Retries are controlled by the orchestrator (not the adapter). The adapter never retries.

## Credit Budget

Unwrangle charges per API call. Credits are surfaced in `remaining_credits` on every response
and logged to `provider_metadata.credits_remaining` in receipts.

Category browse (`lowes_category`) uses 2 credits per query (1 for `lowes_store` + 1 for `lowes_category`).
Keyword search (`lowes_search`) uses 1 credit when the upstream is healthy (currently returns 504 without charging).

## PII Policy

- `zipcode` is not PII — safe to log.
- Store addresses (`address`, `city`, `state`) are NOT logged or included in receipts.
- API key is redacted from all receipts and logs.
