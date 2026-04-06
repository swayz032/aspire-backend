"""Adam Research Platform — Feature Flag Constants.

All new Adam capabilities are gated behind feature flags stored in Supabase.
Flags are evaluated per-tenant via the existing feature_flags service.
Default: OFF (fail-closed per Law #3). Rollback = disable flag (instant).

Rollout order: internal → tenant allowlist → cohort → default on.
"""

from __future__ import annotations

from aspire_orchestrator.services.feature_flags import is_feature_enabled

# ---------------------------------------------------------------------------
# Flag name constants
# ---------------------------------------------------------------------------

# Provider flags — gate new provider clients
ADAM_PROVIDER_ATTOM_V1 = "adam_provider_attom_v1"
ADAM_PROVIDER_SERPAPI_SHOPPING_V1 = "adam_provider_serpapi_shopping_v1"
ADAM_PROVIDER_SERPAPI_HOME_DEPOT_V1 = "adam_provider_serpapi_home_depot_v1"
ADAM_PROVIDER_TRIPADVISOR_V1 = "adam_provider_tripadvisor_v1"
ADAM_PROVIDER_PARALLEL_V1 = "adam_provider_parallel_v1"

# Playbook flags — gate new playbook groups
ADAM_PLAYBOOK_TRAVEL_HOTELS_V1 = "adam_playbook_travel_hotels_v1"

# System flags — gate new subsystems
ADAM_VERIFICATION_V2 = "adam_verification_v2"

# All Adam flags for bulk operations (e.g., rollback all)
ALL_ADAM_FLAGS: list[str] = [
    ADAM_PROVIDER_ATTOM_V1,
    ADAM_PROVIDER_SERPAPI_SHOPPING_V1,
    ADAM_PROVIDER_SERPAPI_HOME_DEPOT_V1,
    ADAM_PROVIDER_TRIPADVISOR_V1,
    ADAM_PROVIDER_PARALLEL_V1,
    ADAM_PLAYBOOK_TRAVEL_HOTELS_V1,
    ADAM_VERIFICATION_V2,
]


async def is_adam_flag_enabled(flag_name: str, tenant_id: str) -> bool:
    """Check an Adam feature flag. Delegates to the global feature_flags service.

    Returns False (fail-closed) if flag missing, disabled, or query fails.
    """
    return await is_feature_enabled(flag_name, tenant_id)
