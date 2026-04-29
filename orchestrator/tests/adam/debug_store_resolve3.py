"""Test store resolver for multiple ZIPs."""
import sys, json; sys.path.insert(0, "src")
from aspire_orchestrator.services.adam.hd_store_resolver import resolve_store

zips = ["30297", "30354", "46201", "33311", "48205", "30274", "30260"]
for zc in zips:
    s = resolve_store(zc)
    if s:
        print("ZIP=%s -> store=%s | %s | zip=%s" % (zc, s["store_id"], s["address"], s["_zip"]))
    else:
        print("ZIP=%s -> NO MATCH" % zc)
