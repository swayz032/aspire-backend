"""Wave 20.6 FULL CHAIN validation on Gulfstream wave-27 project.

Runs the complete pipeline end-to-end:
  Stage 1: extract_project_legends (already populated, idempotent)
  Stage 3b: populate_vector_signature (legends — already populated)
  Stage 3c: cluster_legend_into_families (already populated)
  --- NEW ---
  Stage 4.5: populate_motif_embeddings (Pass 3.2 — closes the gap)
  Stage 5a: cluster_motifs_on_project
  Stage 5b: propagate_legend_bindings_to_clusters
  Stage 6: run_vlm_verification_pass (optional — gated by ASPIRE_VLM_VERIFIER_CAP_USD)

Then queries blueprint_resolved_symbols for the breakthrough KPI:
  total ≥ 3000 → external Modal nuke (Wave 27) ships.

Usage:
    railway run python orchestrator/scripts/validate_wave_20_6_full_chain.py
"""
import asyncio
import sys

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.blueprint.symbol_cluster_engine import (  # noqa: E402
    cluster_motifs_on_project,
    propagate_legend_bindings_to_clusters,
)
from aspire_orchestrator.services.blueprint.symbol_family_embedder import (  # noqa: E402
    populate_motif_embeddings,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"


async def main() -> None:
    print("=== Wave 20.6 FULL CHAIN Validation on Gulfstream ===")
    print(f"Project: {PROJ}")
    print()

    print("--- Stage 4.5: populate_motif_embeddings (Pass 3.2) ---")
    try:
        r1 = await populate_motif_embeddings(project_id=PROJ, suite_id=SUITE)
        print(f"OK: {r1}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return

    print()
    print("--- Stage 5a: cluster_motifs_on_project ---")
    try:
        r2 = await cluster_motifs_on_project(project_id=PROJ, suite_id=SUITE)
        if isinstance(r2, dict):
            print(f"OK: cluster_count={len(r2)}, sample={list(r2.items())[:3]}")
        else:
            print(f"OK: {r2}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return

    print()
    print("--- Stage 5b: propagate_legend_bindings_to_clusters ---")
    try:
        r3 = await propagate_legend_bindings_to_clusters(
            project_id=PROJ, suite_id=SUITE
        )
        print(f"OK: {r3}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return

    print()
    print("=== DONE — query blueprint_resolved_symbols for final KPI ===")


if __name__ == "__main__":
    asyncio.run(main())
