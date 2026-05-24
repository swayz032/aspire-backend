"""Wave 20.12 — full pipeline + consensus VLM validation."""
import asyncio
import sys
sys.path.insert(0, "orchestrator/src")
from aspire_orchestrator.services.blueprint.terminal_resolver import resolve_terminal_states  # noqa: E402
from aspire_orchestrator.services.blueprint.multi_rep_consensus import run_consensus_vlm_pass  # noqa: E402
from aspire_orchestrator.services.supabase_client import supabase_select  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"


async def main() -> None:
    print("=== STEP 1: TR-Layer ===")
    r1 = await resolve_terminal_states(project_id=PROJ, suite_id=SUITE)
    for k in ("BOUND_MATERIAL", "TEXT_OCR", "PLAN_ELEMENT", "CRUFT", "UNKNOWN_REVIEW"):
        print(f"  {k:<20s} rows={r1.get(f'{k}_rows',0):>6d}  inst={r1.get(f'{k}_instances',0):>7d}")

    print()
    print("=== STEP 2: Multi-Rep Consensus VLM ===")
    r2 = await run_consensus_vlm_pass(project_id=PROJ, suite_id=SUITE)
    print(f"  examined={r2['families_examined']}  bound={r2['families_bound']}  rows_updated={r2['rows_updated']}")
    for d in r2.get("per_family", [])[:5]:
        print(f"    family={d['motif_hash'][:10]} legend={d['candidate_legend_label'][:30]} pos={d['positive_ratio']} mean={d['mean_similarity']} var={d['variance']} inst={d['total_instances']} bound={d['bound']}")

    print()
    print("=== STEP 3: Final ===")
    rows: list[dict] = []
    last = ""
    while True:
        cur = f"&id=gt.{last}" if last else ""
        page = await supabase_select(
            "blueprint_resolved_symbols",
            f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}{cur}&order=id", limit=1000,
        )
        if not page:
            break
        rows.extend(page); last = page[-1]["id"]
        if len(page) < 1000:
            break
    cr: dict[str, int] = {}
    ci: dict[str, int] = {}
    for r in rows:
        st = r.get("terminal_state", "UNKNOWN_REVIEW")
        cr[st] = cr.get(st, 0) + 1
        b = r.get("bbox") or {}
        try:
            inst = int(b.get("instance_count", 1)) if isinstance(b, dict) else 1
        except (TypeError, ValueError):
            inst = 1
        ci[st] = ci.get(st, 0) + max(inst, 1)
    print(f"  total rows={sum(cr.values())} inst={sum(ci.values())}")
    for st in ("BOUND_MATERIAL", "DECORATION", "TEXT_OCR", "PLAN_ELEMENT", "CRUFT", "UNKNOWN_REVIEW"):
        print(f"  {st:<20s} rows={cr.get(st,0):>6d}  inst={ci.get(st,0):>7d}")


if __name__ == "__main__":
    asyncio.run(main())
