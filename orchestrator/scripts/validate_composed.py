"""Wave 20.13.1 composed-symbol resolver validation on Gulfstream."""
import asyncio
import sys
sys.path.insert(0, "orchestrator/src")
from aspire_orchestrator.services.blueprint.composed_symbol_resolver import resolve_composed_symbols  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"


async def main() -> None:
    print("=== Wave 20.13.1 Composed Symbol Terminal Resolver — Gulfstream ===")
    r = await resolve_composed_symbols(project_id=PROJ, suite_id=SUITE)
    print(f"composed_total:  {r['composed_total']}")
    print(f"instance_total:  {r['instance_total']}")
    print(f"inserted:        {r['inserted']}")
    print()
    print(f"{'state':<20s} {'rows':>7s} {'inst':>9s}")
    print("-" * 40)
    for state in ("BOUND_MATERIAL", "DECORATION", "TEXT_OCR", "PLAN_ELEMENT", "CRUFT", "UNKNOWN_REVIEW"):
        rows = r.get(f"{state}_rows", 0)
        inst = r.get(f"{state}_instances", 0)
        print(f"{state:<20s} {rows:>7d} {inst:>9d}")


if __name__ == "__main__":
    asyncio.run(main())
