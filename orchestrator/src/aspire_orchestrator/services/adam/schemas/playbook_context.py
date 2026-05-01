"""PlaybookContext — Shared context dataclass for all playbook execute functions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlaybookContext:
    """Execution context passed to every playbook function."""

    suite_id: str
    office_id: str
    correlation_id: str
    capability_token_id: str | None = None
    capability_token_hash: str | None = None
    tenant_id: str = ""
    # Office address coordinates — used for haversine-based store disambiguation
    # (Wave A.5). Populated from suite_profiles in server.py when available.
    office_lat: float | None = None
    office_lng: float | None = None
    # Round 7 A.2 — multi-store opt-in. When True, the trades playbook
    # TOOL_MATERIAL_PRICE_CHECK runs Google Shopping (non-HD retailers) on the
    # voice path AND skips the HD-only result filter so Lowe's/Walmart/Ace/Amazon
    # records survive into the carousel. Default False preserves HD-only voice
    # behavior + latency.
    include_other_stores: bool = False
