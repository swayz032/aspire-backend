"""Direct execution of L-5 fixed tests by calling functions, no pytest discovery.
Bypasses 20-min scipy/pytest discovery overhead on Windows — use when pytest hangs.
Run: `python scripts/win_debug/direct_test.py`"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

results = {}

try:
    from aspire_orchestrator.services.blueprint.semantic_edges.generators.test_locality_generators import (
        test_inside_zone_partial_overlap_routes_to_unknown_review,
        test_annotated_by_far_distance_drops_to_unknown_review,
    )

    for name, fn in [
        ("inside_zone_partial_overlap", test_inside_zone_partial_overlap_routes_to_unknown_review),
        ("annotated_by_far_distance", test_annotated_by_far_distance_drops_to_unknown_review),
    ]:
        try:
            fn()
            results[name] = "PASS"
        except Exception as e:
            results[name] = f"FAIL: {type(e).__name__}: {e}"
            traceback.print_exc()
except Exception as e:
    results["IMPORT"] = f"FAIL: {type(e).__name__}: {e}"
    traceback.print_exc()

out_path = Path(__file__).parent / "_direct_test_result.txt"
out_path.write_text("\n".join(f"{k}: {v}" for k, v in results.items()))
print("=== RESULTS ===")
for k, v in results.items():
    print(f"{k}: {v}")
print(f"\nWritten to: {out_path}")
