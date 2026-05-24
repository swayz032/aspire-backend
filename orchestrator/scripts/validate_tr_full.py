"""Full Wave 20.8 + 20.8.1 + 20.8.2 validation: TR-Layer + VLM tiebreaker.

Steps:
  1. Run resolve_terminal_states (TR-Layer family-level routing)
  2. Run run_tr_vlm_tiebreaker (VLM verify high-instance UNKNOWN families)
  3. Re-query final terminal_state distribution from blueprint_resolved_symbols

Final BOUND_MATERIAL = deterministic-bound + vlm-verified bindings.
"""
import asyncio
import sys
sys.path.insert(0, "orchestrator/src")
from aspire_orchestrator.services.blueprint.terminal_resolver import (  # noqa: E402
    ALL_STATES,
    resolve_terminal_states,
)
from aspire_orchestrator.services.blueprint.tr_vlm_tiebreaker import (  # noqa: E402
    run_tr_vlm_tiebreaker,
)
from aspire_orchestrator.services.supabase_client import supabase_select  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"


async def main() -> None:
    print("=== STEP 1: TR-Layer (family-level deterministic routing) ===")
    r1 = await resolve_terminal_states(project_id=PROJ, suite_id=SUITE)
    print(f"  families: {r1['families_total']}  motifs: {r1['total_motif_rows']}  inst: {r1['total_instances']}")
    for state in ALL_STATES:
        rows = r1.get(f"{state}_rows", 0)
        inst = r1.get(f"{state}_instances", 0)
        print(f"  {state:<20s} rows={rows:>6d}  inst={inst:>8d}")

    print()
    print("=== STEP 2: VLM Tiebreaker (UNKNOWN families >=25 instances) ===")
    r2 = await run_tr_vlm_tiebreaker(project_id=PROJ, suite_id=SUITE, correlation_id="wave-20.8.2")
    print(f"  examined={r2['families_examined']}  marked_for_vlm={r2['families_marked_for_vlm']}  rows_updated={r2.get('rows_updated', 0)}")
    print(f"  vlm_result: {r2['vlm_result']}")

    print()
    print("=== STEP 3: Final terminal_state distribution ===")
    # Paginate — Supabase REST hard-caps at 1000/request
    rows: list[dict] = []
    last_id = ""
    while True:
        cur = f"&id=gt.{last_id}" if last_id else ""
        page = await supabase_select(
            "blueprint_resolved_symbols",
            f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}{cur}&order=id",
            limit=1000,
        )
        if not page:
            break
        rows.extend(page)
        last_id = page[-1]["id"]
        if len(page) < 1000:
            break
    counts_rows: dict[str, int] = {}
    counts_inst: dict[str, int] = {}
    for r in rows or []:
        st = r.get("terminal_state", "UNKNOWN_REVIEW")
        counts_rows[st] = counts_rows.get(st, 0) + 1
        bbox = r.get("bbox") or {}
        try:
            inst = int(bbox.get("instance_count", 1)) if isinstance(bbox, dict) else 1
        except (TypeError, ValueError):
            inst = 1
        counts_inst[st] = counts_inst.get(st, 0) + max(inst, 1)

    total_rows = sum(counts_rows.values())
    total_inst = sum(counts_inst.values())
    print(f"  TOTAL rows={total_rows} inst={total_inst}")
    print(f"  {'state':<20s} {'rows':>7s} {'rows%':>7s} {'inst':>9s} {'inst%':>7s}")
    print("  " + "-" * 53)
    for state in ALL_STATES:
        r = counts_rows.get(state, 0)
        i = counts_inst.get(state, 0)
        rp = (r / total_rows * 100) if total_rows else 0
        ip = (i / total_inst * 100) if total_inst else 0
        print(f"  {state:<20s} {r:>7d} {rp:>6.1f}% {i:>9d} {ip:>6.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
