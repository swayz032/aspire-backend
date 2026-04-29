"""Debug: Why does 30297 resolve to Ellenwood instead of Jonesboro?"""
import sys
sys.path.insert(0, "src")
from aspire_orchestrator.services.adam.hd_store_resolver import resolve_store, _load_stores, _PREFIX_INDEX

s = resolve_store("30297")
sid = s.get("store_id", "?") if s else "NONE"
addr = s.get("address", "?") if s else "NONE"
zc = s.get("postal_code", "?") if s else "NONE"
print(f"Resolved: store_id={sid} | {addr} | zip={zc}")

# Show all 302xx candidates
_load_stores()
candidates = _PREFIX_INDEX.get("302", [])
target = 30297
candidates.sort(key=lambda x: abs(int(x["_zip"]) - target))
print(f"\nAll 302xx stores sorted by ZIP distance to 30297:")
for c in candidates[:10]:
    diff = abs(int(c["_zip"]) - target)
    sid = c["store_id"]
    addr = c["address"]
    zc = c["_zip"]
    print(f"  store={sid} | zip={zc} | diff={diff} | {addr}")
