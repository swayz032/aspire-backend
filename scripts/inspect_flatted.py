#!/usr/bin/env python3
"""Properly parse flatted format from n8n execution data."""
import json
import urllib.request
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

N8N_BASE = "http://localhost:5678"

# Login
data = json.dumps({
    'emailOrLdapLoginId': 'admin@aspireos.app',
    'password': 'AspireN8N2026!'
}).encode()
req = urllib.request.Request(f"{N8N_BASE}/rest/login", data=data, headers={'Content-Type': 'application/json'})
resp = urllib.request.urlopen(req, timeout=10)
cookie = None
for c in (resp.headers.get_all("Set-Cookie") or []):
    if "n8n-auth" in c:
        cookie = c.split(";")[0]
        break

def unflatten(flat_str):
    """Unflatten a flatted JSON string."""
    arr = json.loads(flat_str)
    if not isinstance(arr, list) or len(arr) == 0:
        return arr

    # Build a lookup: each element can be a string index reference or actual value
    result = [None] * len(arr)
    visited = [False] * len(arr)

    def resolve(idx):
        if visited[idx]:
            return result[idx]
        visited[idx] = True

        val = arr[idx]
        if isinstance(val, str):
            # Could be an index reference or an actual string
            # In flatted format, strings that look like numbers are index refs
            # but the first element (index 0) is the root
            result[idx] = val
        elif isinstance(val, dict):
            obj = {}
            for k, v in val.items():
                if isinstance(v, str) and v.isdigit():
                    obj[k] = resolve(int(v))
                else:
                    obj[k] = v
            result[idx] = obj
        elif isinstance(val, list):
            lst = []
            for item in val:
                if isinstance(item, str) and item.isdigit():
                    lst.append(resolve(int(item)))
                else:
                    lst.append(item)
            result[idx] = lst
        else:
            result[idx] = val

        return result[idx]

    return resolve(0)


# Fetch executions 87 (valid HMAC) and 95 (invalid HMAC)
for exec_id in [87, 95]:
    print(f"\n{'='*80}")
    print(f"EXECUTION {exec_id}")
    print(f"{'='*80}")

    req = urllib.request.Request(f"{N8N_BASE}/rest/executions/{exec_id}", headers={"Cookie": cookie})
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read().decode()

    # The response has a "data" field which contains the execution data
    outer = json.loads(raw)
    exec_obj = outer.get("data", outer)

    # executionData is in exec_obj["data"] and is in flatted format
    exec_data_str = exec_obj.get("data")
    if not exec_data_str:
        print("No execution data found")
        continue

    if isinstance(exec_data_str, str):
        # It's flatted - need to unflatten
        try:
            exec_data = unflatten(exec_data_str)
        except Exception as e:
            print(f"Unflatten failed: {e}")
            # Fall back to string search
            exec_data = None
    else:
        exec_data = exec_data_str

    if exec_data and isinstance(exec_data, dict):
        result_data = exec_data.get("resultData", {})
        if isinstance(result_data, dict):
            run_data = result_data.get("runData", {})
            if isinstance(run_data, dict):
                for node_name in run_data:
                    node_runs = run_data[node_name]
                    if isinstance(node_runs, list):
                        for i, run in enumerate(node_runs):
                            if isinstance(run, dict):
                                main_data = run.get("data", {})
                                if isinstance(main_data, dict):
                                    main = main_data.get("main", [])
                                    if isinstance(main, list):
                                        for branch_idx, branch in enumerate(main):
                                            if isinstance(branch, list):
                                                for item in branch[:2]:
                                                    if isinstance(item, dict):
                                                        j = item.get("json", {})
                                                        if isinstance(j, dict):
                                                            # Print interesting fields
                                                            keys = list(j.keys())[:20]
                                                            print(f"\n  Node: {node_name} (branch {branch_idx})")
                                                            print(f"  Keys: {keys}")
                                                            if "killed" in j:
                                                                print(f"  killed: {j['killed']}")
                                                            if "reason" in j:
                                                                print(f"  reason: {j['reason']}")
                                                            if "headers" in j and isinstance(j["headers"], dict):
                                                                sig = j["headers"].get("x-webhook-signature", "N/A")
                                                                print(f"  x-webhook-signature: {sig}")
                                                            if "body" in j and isinstance(j["body"], dict):
                                                                print(f"  body: {json.dumps(j['body'])[:200]}")
                                                            if "suiteId" in j:
                                                                print(f"  suiteId: {j['suiteId']}")
                                                            if "requestId" in j:
                                                                print(f"  requestId: {j['requestId']}")
    else:
        # String search fallback
        print("Falling back to string search...")
        # Look for the output of Kill Switch node
        for kw in ['killed', 'hmac_validation_failed', 'retest-intake', 'sha256=']:
            idx = raw.find(kw)
            if idx >= 0:
                print(f"  [{kw}] at {idx}: ...{raw[max(0,idx-40):idx+80]}...")
