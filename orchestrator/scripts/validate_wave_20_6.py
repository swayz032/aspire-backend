"""Wave 20.6 end-to-end validation on Gulfstream wave-27 project.

Chain: Stage 1 (legend extract) -> Stage 3b (vector signature populate) -> Stage 3c (cluster families).
Then queries blueprint_project_legends + blueprint_resolved_symbols for KPI numbers.

Usage:
    railway run python orchestrator/scripts/validate_wave_20_6.py
"""
import asyncio
import sys

# Ensure src is on path
sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.blueprint.legend_extractor import extract_project_legends  # noqa: E402
from aspire_orchestrator.services.blueprint.symbol_family_embedder import (  # noqa: E402
    cluster_legend_into_families,
    populate_vector_signature,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE = "94b89098-c4bf-4419-a154-e18d9d53f993"


async def main() -> None:
    print("=== Wave 20.6 End-to-End Validation on Gulfstream ===")
    print(f"Project: {PROJ}")
    print(f"Suite: {SUITE}")
    print()

    print("--- Stage 1: extract_project_legends ---")
    try:
        r1 = await extract_project_legends(
            project_id=PROJ,
            suite_id=SUITE,
            office_id=OFFICE,
            correlation_id="wave-20-6-validation",
        )
        print(f"OK: {r1}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")

    print()
    print("--- Stage 3b: populate_vector_signature ---")
    try:
        r2 = await populate_vector_signature(project_id=PROJ, suite_id=SUITE)
        print(f"OK: {r2}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")

    print()
    print("--- Stage 3c: cluster_legend_into_families ---")
    try:
        r3 = await cluster_legend_into_families(
            project_id=PROJ, suite_id=SUITE, similarity_threshold=0.92
        )
        print(f"OK: families={len(r3)}, summary={ {k: len(v) for k, v in list(r3.items())[:5]} }")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
