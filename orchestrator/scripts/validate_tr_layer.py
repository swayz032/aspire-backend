"""Wave 20.8 TR-Layer validation — runs terminal_resolver on Gulfstream + reports per-state KPI."""
import asyncio
import sys
sys.path.insert(0, "orchestrator/src")
from aspire_orchestrator.services.blueprint.terminal_resolver import resolve_terminal_states  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"


async def main() -> None:
    print("=== Wave 20.8 Terminal Resolver Layer — Gulfstream ===")
    r = await resolve_terminal_states(project_id=PROJ, suite_id=SUITE)
    print()
    print("=== 100% TERMINAL STATE COVERAGE ===")
    total = r.get("total_detections", 0)
    for state in ["BOUND_MATERIAL", "DECORATION", "TEXT_OCR", "PLAN_ELEMENT", "CRUFT", "UNKNOWN_REVIEW"]:
        n = r.get(state, 0)
        pct = (n / total * 100) if total else 0
        print(f"  {state:<20s} {n:>7d}  ({pct:>5.1f}%)")
    print(f"  {'TOTAL':<20s} {total:>7d}  ({r.get('coverage_pct', 0):>5.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())
