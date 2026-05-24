"""Wave 20.8 TR-Layer validation — family-level routing on Gulfstream."""
import asyncio
import sys
sys.path.insert(0, "orchestrator/src")
from aspire_orchestrator.services.blueprint.terminal_resolver import (  # noqa: E402
    ALL_STATES,
    resolve_terminal_states,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"


async def main() -> None:
    print("=== Wave 20.8.1 Terminal Resolver (family-level) — Gulfstream ===")
    r = await resolve_terminal_states(project_id=PROJ, suite_id=SUITE)
    print(f"families: {r['families_total']}")
    print(f"motif_rows: {r['total_motif_rows']}")
    print(f"instances: {r['total_instances']}")
    print()
    print("=== TERMINAL STATE DISTRIBUTION ===")
    print(f"{'state':<20s} {'rows':>7s} {'rows%':>7s} {'inst':>8s} {'inst%':>7s}")
    print("-" * 55)
    total_rows = r["total_motif_rows"]
    total_inst = r["total_instances"]
    for state in ALL_STATES:
        rows = r.get(f"{state}_rows", 0)
        inst = r.get(f"{state}_instances", 0)
        row_pct = (rows / total_rows * 100) if total_rows else 0
        inst_pct = (inst / total_inst * 100) if total_inst else 0
        print(f"{state:<20s} {rows:>7d} {row_pct:>6.1f}% {inst:>8d} {inst_pct:>6.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
