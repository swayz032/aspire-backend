"""Materials playbook — regression lock (Pass H v1).

File-content invariants for the bug fixes shipped 2026-05-12 → 2026-05-13.
These tests don't exercise runtime behavior — they guarantee that the
specific anti-patterns we removed can't sneak back via a future refactor.

Each lock points at the bug it prevents AND the production incident or
founder direction that motivated it. If a lock fails, READ THE FAILURE
MESSAGE — it explains which decision is being violated and why it exists.
If the design intent legitimately changes, update BOTH this file and the
underlying code in the SAME commit. Never delete-only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_BACKEND_ROOT / rel).read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────
# Lock #1: route forwards `address` AND `user_address` to the playbook
#
# 2026-05-12 incident (PR #58 + #59): the materials route extracted the
# ZIP from the address but never passed the raw address to the playbook
# → playbook's Round 4 (Google Places nearest-HD resolution) was skipped
# entirely because `user_address=""` → SerpApi was called with bare ZIP
# only → for residential ZIPs (Forest Park 30297, Tallahassee 32303) the
# directory missed → SerpApi defaulted pickup to Bangor 2414.
# Fix: routes/materials.py now passes `user_address=address or ""`.
# ─────────────────────────────────────────────────────────────────────
class TestRouteWiresUserAddressToPlaybook:
    src = _read("orchestrator/src/aspire_orchestrator/routes/materials.py")

    def test_route_passes_user_address_to_playbook(self):
        """execute_tool_material_price_check must receive user_address."""
        # The call site uses keyword args; match the kwarg explicitly.
        assert "user_address=address or" in self.src, (
            "Route lost the `user_address=address or \"\"` kwarg — the playbook's "
            "Places-based Round 4 resolution will silently skip and pickup "
            "data will default to Bangor 2414 again. Restore the kwarg or "
            "update this lock if architecture has changed."
        )

    def test_route_accepts_address_query_param(self):
        assert 'address: str | None = Query(None' in self.src or 'address: Optional[str] = Query(None' in self.src, (
            "Materials route dropped the `address` query parameter declaration."
        )


# ─────────────────────────────────────────────────────────────────────
# Lock #2: playbook Round 4 derives store_id from postal_code
#
# 2026-05-13 incident: Round 4 resolved nearest_store via Google Places
# AND set zip_code from nearest_store.postal_code — but never derived
# the HD-internal numeric store_id. SerpApi got delivery_zip alone and
# defaulted pickup data to the account's default store (Bangor 2414).
# Fix: commit 4533bf6 — after nearest_store resolves, look up store_id
# from postal_code via lookup_store_by_zip_code.
# ─────────────────────────────────────────────────────────────────────
class TestPlaybookDerivesStoreIdFromPostalCode:
    src = _read(
        "orchestrator/src/aspire_orchestrator/services/adam/playbooks/trades.py"
    )

    def test_round_4_calls_find_nearest_home_depot_by_address(self):
        assert "find_nearest_home_depot_by_address(" in self.src, (
            "Playbook Round 4 was removed — without it, residential addresses "
            "(any zip not in our 1,776-store directory) can't resolve to a "
            "real HD store. SerpApi will default to Bangor."
        )

    def test_round_4_imports_lookup_store_by_zip_code(self):
        assert "lookup_store_by_zip_code" in self.src, (
            "Playbook stopped importing lookup_store_by_zip_code — the "
            "postal_code → store_id derivation can't run, SerpApi will "
            "default pickup data to Bangor 2414."
        )

    def test_round_4_uses_lookup_to_derive_store_id(self):
        # Must invoke the lookup AND assign the result into store_id.
        # The actual code: `_hd_record = lookup_store_by_zip_code(zip_code)`
        # followed by `store_id = str(_hd_record.get("store_id", ""))`.
        assert "lookup_store_by_zip_code(zip_code)" in self.src, (
            "Round 4 lost the store_id derivation call. Without this, "
            "SerpApi gets only delivery_zip → pickup defaults to Bangor."
        )
        # And the derivation must conditionally update store_id.
        assert 'store_id = str(_hd_record.get("store_id"' in self.src or \
               "store_id = str(_hd_record.get('store_id'" in self.src, (
            "Round 4 lost the `store_id = str(_hd_record.get(...))` assignment."
        )


# ─────────────────────────────────────────────────────────────────────
# Lock #3: hd_store_directory has the ZIP-prefix proximity fallback
#
# 2026-05-13 founder direction: "users cant depend on a static directory."
# Residential ZIPs (32303 Tallahassee, 30297 Forest Park) don't anchor an
# HD store, but the nearest HD is always in the same USPS SCF (first
# 3 digits). Without prefix fallback, exact-miss = no store_id = Bangor
# pickup. Fix: commit a02a23f.
# ─────────────────────────────────────────────────────────────────────
class TestDirectoryHasZipPrefixFallback:
    src = _read(
        "orchestrator/src/aspire_orchestrator/services/adam/hd_store_directory.py"
    )

    def test_lookup_falls_back_to_prefix_match_when_exact_misses(self):
        # The fallback uses `prefix = zc[:3]` and searches the cache for
        # any zip starting with that prefix.
        assert "prefix = zc[:3]" in self.src, (
            "ZIP-prefix proximity fallback was removed from "
            "lookup_store_by_zip_code. Residential ZIPs that don't anchor "
            "an HD will return None → playbook can't derive store_id → "
            "SerpApi defaults pickup to Bangor 2414."
        )
        assert ".startswith(prefix)" in self.src, (
            "Prefix-match loop was removed — see the function docstring "
            "for the design rationale (USPS SCF = same metro)."
        )

    def test_lookup_returns_exact_match_first_before_prefix_fallback(self):
        """Exact 5-digit hit must short-circuit BEFORE the prefix search.

        Otherwise we'd hit the slower path needlessly for every call, and
        a 5-digit ZIP that's a perfect HD anchor would get displaced by
        a numerically-closer-but-different-HD ZIP in the prefix loop.
        """
        # The function should have `record = cache.get(zc)` BEFORE the
        # prefix-match block.
        idx_exact = self.src.find("cache.get(zc)")
        idx_prefix = self.src.find("prefix = zc[:3]")
        assert idx_exact != -1 and idx_prefix != -1, "lock anchors missing"
        assert idx_exact < idx_prefix, (
            "Exact match must short-circuit before the prefix fallback."
        )


# ─────────────────────────────────────────────────────────────────────
# Lock #4: Yelp adapter passes thumbnail through to the response
#
# 2026-05-13 founder feedback: "google place even do pictures" — supplier
# cards need a photo at the top. SerpApi Yelp returns `thumbnail` (CDN
# URL, public retail data) but our adapter dropped it. Fix: commit 5e8880c.
# ─────────────────────────────────────────────────────────────────────
class TestYelpAdapterShipsThumbnail:
    src = _read(
        "orchestrator/src/aspire_orchestrator/providers/serpapi_yelp_client.py"
    )

    def test_normalize_business_emits_thumbnail_field(self):
        # The return dict must include a "thumbnail" key sourced from
        # biz.get("thumbnail").
        assert '"thumbnail": thumbnail' in self.src or "'thumbnail': thumbnail" in self.src, (
            "Yelp adapter lost the thumbnail field in its normalized output. "
            "Supplier cards will fall back to category-icon tiles → premium "
            "feel collapses. Restore the field or update this lock."
        )


# ─────────────────────────────────────────────────────────────────────
# Meta: the materials route registration is intact.
#
# Defensive: if someone moves the route file or renames it, the rest of
# these locks become silent no-ops. Verify the route is still mounted.
# ─────────────────────────────────────────────────────────────────────
class TestMaterialsRouteIsMounted:
    src = _read("orchestrator/src/aspire_orchestrator/server.py")

    def test_materials_router_is_imported_and_included(self):
        assert "materials" in self.src, (
            "server.py no longer references the materials router — search "
            "endpoint is unreachable. Re-register or update this lock."
        )
