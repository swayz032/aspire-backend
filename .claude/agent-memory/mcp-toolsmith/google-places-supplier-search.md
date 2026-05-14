---
name: Google Places Supplier Search Patterns
description: Places v1 searchText adapter for supplier-mode materials search — field masks, photo proxy, fallback chain, PII redaction
type: project
---

## Verified patterns from Pass E upgrade (2026-05-13)

### Field mask
Tight field mask across Basic + Preferred + Contact billing tiers:
```
places.id, places.displayName, places.formattedAddress, places.shortFormattedAddress,
places.location, places.types, places.rating, places.userRatingCount,
places.currentOpeningHours, places.regularOpeningHours,
places.nationalPhoneNumber, places.internationalPhoneNumber, places.websiteUri, places.photos
```
Omit `establishment` and `point_of_interest` from type lists when building categories — they are generic and pollute the display.

### Photo proxy (THREAT-004)
Import `_PLACES_PHOTO_PROXY_PATH` from `places_nearest_finder`. Emit:
`/v1/places/photo?ref=<url-encoded resource name>&maxHeightPx=400&maxWidthPx=600`
Never embed `key=` in any client-visible URL.

### Fallback chain design
- `_search_suppliers` route function tries `search_suppliers_via_places` first.
- If Places returns **>=2** suppliers → use them, write to cache under `provider="google_places"`.
- If Places returns **<2** (or raises) → fall back to `execute_serpapi_yelp_search`, cache under `provider="serpapi_yelp"`.
- Cache hit check iterates both `("google_places", "serpapi_yelp")` on first pass — whichever was cached last wins.
- Receipt `redacted_outputs.engine` is `"google_places"` or `"yelp"` — used to audit adoption over time.

### The `<2` threshold
A single Places result is often a GCP geocoding miss (Places matched something irrelevant). Two results gives sufficient signal that Places found real suppliers.

### Address parsers
`_parse_zip`, `_parse_state`, `_parse_city` are in `google_places_supplier_search.py`. They reuse `_POSTAL_RE` from `places_nearest_finder`. The state regex is: `,\s*([A-Z]{2})\s+\d{5}`. The city regex matches the token before state+ZIP.

### PII discipline
- `_redact_address` (imported from `places_nearest_finder`) hashes location strings before any logger call.
- Phone numbers are in result payload (public retail data carve-out, same as Yelp) — NOT in receipt `redacted_outputs`.
- API key: never in URLs, never in logs. Settings access via `getattr(settings, "google_maps_api_key", "")`.

### distance_miles
Always `None` from this adapter (F-MED-7 precedent). We don't have user coordinates, so we emit `None` not `0.0`.

### Error handling hierarchy
429 → log QUOTA_EXCEEDED, return []. 403 → log PLACES_DISABLED, return []. 5xx → log SERVER_ERROR, return []. API error body (`error.status == RESOURCE_EXHAUSTED`) → return []. All paths return `[]`, never raise. Outer `asyncio.wait_for` catches `TimeoutError` and also returns `[]`.

### Test patterns that worked
- `patch.object(mod, "_execute_supplier_search", AsyncMock(...))` for unit tests of the public wrapper.
- `patch.object(materials_route, "search_suppliers_via_places", ...)` for route-level fallback tests.
- Do NOT use `patch("....__globals__", {})` on async functions in Python 3.13 — `__globals__` is readonly.
- `_mock_places_client` helper: AsyncMock with `__aenter__`/`__aexit__` + `mock.post = AsyncMock(return_value=mock_resp)`.

**Why:** Google Places returns phone/website/hours in one call, eliminating the Yelp detail-page round-trip (1 SerpApi tick × N cards). Places uses $200/mo Google quota vs 240/account SerpApi cap. Live, authoritative data vs static directory.
