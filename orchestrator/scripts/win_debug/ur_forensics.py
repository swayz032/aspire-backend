"""N-4.5 UNKNOWN_REVIEW forensics — classify the 41 borderline cases."""
import json
import collections
import pathlib

d = json.loads(
    pathlib.Path("scripts/win_debug/_audit_report.json").read_text(encoding="utf-8")
)
ur = d["ur_samples"]
print(f"Total UR captured: {len(ur)}")
print()

all_causes = collections.Counter()
no_cause = 0
for s in ur:
    if not s.get("primary_cause"):
        no_cause += 1
    for c in s.get("primary_cause", []):
        all_causes[c] += 1
print(f"UR with no detected cause: {no_cause}")
print()
print("=== Primary cause frequency (a UR may have multiple causes) ===")
for cause, n in all_causes.most_common():
    pct = 100 * n / max(len(ur), 1)
    print(f"  {cause:<30} : {n:>3}  ({pct:.0f}%)")
print()

print("=== Final score distribution ===")
buckets = collections.Counter(round(s["final_score"], 1) for s in ur)
for k in sorted(buckets.keys()):
    print(f"  {k:.1f} : {buckets[k]}")
print()

print("=== OCR reasoning ===")
for k, v in collections.Counter(s.get("ocr_reason", "None") for s in ur).most_common():
    print(f"  {k:<35} : {v}")
print()

print("=== Per-motif-sheet ===")
for k, v in collections.Counter(s["motif_sheet"] for s in ur).most_common():
    print(f"  {k:<10} : {v}")
print()

print("=== Topology score distribution ===")
for k, v in collections.Counter(s.get("topo_score") for s in ur).most_common():
    print(f"  {k} : {v}")
print()

print("=== All 41 UR cases (sorted by final score desc) ===")
for s in sorted(ur, key=lambda x: -x["final_score"]):
    sid = s["legend_symbol_id"]
    label = s["legend_label"]
    sheet = s["motif_sheet"]
    fs = s["final_score"]
    oc = s["ocr_score"]
    ocr = s["ocr_reason"]
    tp = s["topo_score"]
    cn = s["conn_score"]
    causes = s["primary_cause"]
    print(f"  [{fs}] motif@{sheet} -> sid={sid!r}  label={label!r}")
    print(f"         ocr={oc} ({ocr})  topo={tp}  conn={cn}")
    print(f"         causes: {causes}")
