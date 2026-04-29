"""Test store resolver — show top 5 nearest to 30297."""
import sys; sys.path.insert(0, "src")
from aspire_orchestrator.services.adam.hd_store_resolver import resolve_store, _load_stores, _PREFIX_INDEX

s = resolve_store("30297")
print("Resolved:", s["store_id"], "|", s["address"], "|", s["_zip"])

_load_stores()
candidates = []
for off in range(-3, 4):
    adj = str(int("302") + off).zfill(3)
    candidates.extend(_PREFIX_INDEX.get(adj, []))
target = 30297
candidates.sort(key=lambda c: abs(int(c["_zip"]) - target))
print("\nTop 5 nearest to 30297:")
for c in candidates[:5]:
    d = abs(int(c["_zip"]) - target)
    print("  store=%s | zip=%s | diff=%d | %s" % (c["store_id"], c["_zip"], d, c["address"]))
