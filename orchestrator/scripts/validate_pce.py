"""Wave 20.13 PCE validation — assembles primitives into composed symbols on Gulfstream."""
import asyncio
import sys
sys.path.insert(0, "orchestrator/src")
from aspire_orchestrator.services.blueprint.primitive_composition_engine import (  # noqa: E402
    compose_primitives_into_symbols,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"


async def main() -> None:
    print("=== Wave 20.13 Primitive Composition Engine — Gulfstream ===")
    r = await compose_primitives_into_symbols(project_id=PROJ, suite_id=SUITE)
    print(f"sheets:           {r['sheets']}")
    print(f"primitives:       {r['primitives']}")
    print(f"components:       {r['components']}")
    print(f"composed_symbols: {r['composed_symbols']} (persisted: {r['inserted']})")
    print(f"rejected:         {r['rejected']}")
    print(f"rejection_breakdown: {r['rejection_breakdown']}")


if __name__ == "__main__":
    asyncio.run(main())
