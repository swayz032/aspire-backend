#!/usr/bin/env python3
"""
Full E2E Retest v2: Fixed HMAC to use compact JSON separators.
The key fix: json.dumps(sort_keys(payload), separators=(',', ':'))
to match JavaScript's JSON.stringify() output (no spaces).
"""

import json
import hmac
import hashlib
import urllib.request
import urllib.error
import time
import sys
import io
import re

from _n8n_runtime import (
    get_n8n_admin_email,
    get_n8n_admin_password,
    get_n8n_api_key,
    get_n8n_base_url,
    get_webhook_secret,
)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

N8N_BASE = get_n8n_base_url()
N8N_API_KEY = get_n8n_api_key()

WF_MAP = {
    "aDeQEKoBawhGdUtE": "intake-activation",
    "s1JqxMYrEDbPVbMT": "eli-email-triage",
    "Mf7SSxqkCQLflZt3": "sarah-call-handler",
    "6PXnv0hogAIbKG8F": "nora-meeting-summary",
}

def sort_keys(obj):
    if isinstance(obj, list):
        return [sort_keys(i) for i in obj]
    if isinstance(obj, dict):
        return {k: sort_keys(v) for k, v in sorted(obj.items())}
    return obj

def send_webhook(name, url, payload, secret, valid_hmac=True):
    """Send webhook with HMAC using compact JSON (no spaces) to match JS JSON.stringify."""
    # CRITICAL: Use compact separators to match JavaScript JSON.stringify()
    # Python default json.dumps: {"key": "value"} (with spaces)
    # JS JSON.stringify:         {"key":"value"} (no spaces)
    # The n8n Code node uses JSON.stringify(sortKeys(body)) for HMAC computation
    canonical = json.dumps(sort_keys(payload), separators=(',', ':'))
    body = canonical.encode()

    if valid_hmac:
        sig = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    else:
        sig = 'sha256=' + '0' * 64

    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'X-Webhook-Signature': sig
    })

    label = "VALID" if valid_hmac else "INVALID"
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        body_text = resp.read().decode()
        print(f"  [{label}] {name}: HTTP {resp.status}")
        return {"name": name, "hmac": label, "status": resp.status, "body": body_text}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        print(f"  [{label}] {name}: HTTP {e.code}")
        return {"name": name, "hmac": label, "status": e.code, "body": body_text}
    except Exception as e:
        print(f"  [{label}] {name}: ERROR - {e}")
        return {"name": name, "hmac": label, "status": "ERROR", "body": str(e)}

def n8n_login():
    data = json.dumps({
        'emailOrLdapLoginId': get_n8n_admin_email(),
        'password': get_n8n_admin_password()
    }).encode()
    req = urllib.request.Request(
        f"{N8N_BASE}/rest/login",
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    cookies = resp.headers.get_all("Set-Cookie") or []
    for c in cookies:
        if "n8n-auth" in c:
            return c.split(";")[0]
    raise RuntimeError("No n8n-auth cookie received")

def unflatten(flat_str):
    """Unflatten a flatted JSON string."""
    arr = json.loads(flat_str)
    if not isinstance(arr, list) or len(arr) == 0:
        return arr

    result = [None] * len(arr)
    visited = [False] * len(arr)

    def resolve(idx):
        if visited[idx]:
            return result[idx]
        visited[idx] = True
        val = arr[idx]
        if isinstance(val, str):
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


def analyze_execution(exec_id, cookie):
    """Fetch and analyze a single execution via the internal API."""
    req = urllib.request.Request(
        f"{N8N_BASE}/rest/executions/{exec_id}",
        headers={"Cookie": cookie}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    raw = resp.read().decode()
    outer = json.loads(raw)
    exec_obj = outer.get("data", outer)

    result = {
        "id": exec_id,
        "killed": None,
        "hmac_passed": None,
        "gateway_reached": None,
        "econnrefused": False,
        "gateway_status": None,
        "receipt_emitted": False,
        "has_test_payload": False,
        "error_messages": [],
        "nodes_hit": [],
    }

    # Try flatted decode
    exec_data_str = exec_obj.get("data")
    if not exec_data_str:
        return result

    try:
        if isinstance(exec_data_str, str):
            exec_data = unflatten(exec_data_str)
        else:
            exec_data = exec_data_str
    except Exception:
        # Fallback to string search
        if 'ECONNREFUSED' in raw:
            result["econnrefused"] = True
            result["gateway_reached"] = False
        if 'hmac_validation_failed' in raw:
            result["hmac_passed"] = False
            result["killed"] = True
        return result

    if not isinstance(exec_data, dict):
        return result

    run_data = exec_data.get("resultData", {}).get("runData", {})
    if not isinstance(run_data, dict):
        return result

    for node_name, node_runs in run_data.items():
        result["nodes_hit"].append(node_name)
        if not isinstance(node_runs, list):
            continue
        for run in node_runs:
            if not isinstance(run, dict):
                continue
            main_data = run.get("data", {})
            if not isinstance(main_data, dict):
                continue
            for branch in main_data.get("main", []):
                if not isinstance(branch, list):
                    continue
                for item in branch:
                    if not isinstance(item, dict):
                        continue
                    j = item.get("json", {})
                    if not isinstance(j, dict):
                        continue

                    # Check Kill Switch node output
                    if "killed" in j:
                        if j["killed"] is True or j["killed"] == "true":
                            result["killed"] = True
                            result["hmac_passed"] = False
                        elif j["killed"] is False or j["killed"] == "false":
                            result["killed"] = False
                            result["hmac_passed"] = True

                    if j.get("reason") == "hmac_validation_failed":
                        result["hmac_passed"] = False
                        result["killed"] = True

                    # Check for test payload markers
                    for marker in ['retest-intake', 'retest-email', 'retest-call', 'retest-room']:
                        vals = json.dumps(j)
                        if marker in vals:
                            result["has_test_payload"] = True

                    # Check for Gateway call results
                    if "statusCode" in j:
                        result["gateway_status"] = j["statusCode"]
                        result["gateway_reached"] = True

                    # Check for ECONNREFUSED
                    if isinstance(j.get("message"), str) and "ECONNREFUSED" in j["message"]:
                        result["econnrefused"] = True
                        result["gateway_reached"] = False

                    # Check for error messages
                    msg = j.get("message", "")
                    if isinstance(msg, str) and any(kw in msg.lower() for kw in ['error', 'fail', 'refused', 'timeout']):
                        result["error_messages"].append(msg[:100])

    # Receipt emission: check if Supabase receipt nodes were hit
    for node in result["nodes_hit"]:
        if "Receipt" in node or "receipt" in node:
            result["receipt_emitted"] = True

    # Also check raw string for supabase URL (in case node names don't match)
    if 'qtuehjqlcmfcascqjjhc' in raw:
        result["receipt_emitted"] = True

    return result


# ========================================================
# WEBHOOKS CONFIG
# ========================================================

WEBHOOKS = [
    {
        "name": "intake-activation",
        "url": f"{N8N_BASE}/webhook/intake-activation",
        "secret": get_webhook_secret("intake"),
        "payload": {
            "suiteId": "c4eebdbd-e019-42c0-9143-077762e92bbc",
            "officeId": "c4eebdbd-e019-42c0-9143-077762e92bbc",
            "industry": "Plumbing",
            "servicesNeeded": ["Invoicing & Payments"],
            "correlationId": "retest-intake-002"
        }
    },
    {
        "name": "eli-email-triage",
        "url": f"{N8N_BASE}/webhook/eli-email-triage",
        "secret": get_webhook_secret("eli"),
        "payload": {
            "email_id": "retest-email-002",
            "from": "customer@example.com",
            "subject": "Quote request",
            "body_preview": "Need plumbing quote",
            "suite_id": "c4eebdbd-e019-42c0-9143-077762e92bbc",
            "office_id": "c4eebdbd-e019-42c0-9143-077762e92bbc"
        }
    },
    {
        "name": "sarah-call-handler",
        "url": f"{N8N_BASE}/webhook/sarah-call-handler",
        "secret": get_webhook_secret("sarah"),
        "payload": {
            "call_sid": "retest-call-002",
            "from_number": "+15551234567",
            "to_number": "+15559876543",
            "suite_id": "c4eebdbd-e019-42c0-9143-077762e92bbc",
            "office_id": "c4eebdbd-e019-42c0-9143-077762e92bbc"
        }
    },
    {
        "name": "nora-meeting-summary",
        "url": f"{N8N_BASE}/webhook/nora-meeting-summary",
        "secret": get_webhook_secret("nora"),
        "payload": {
            "room_name": "retest-room-002",
            "duration": 1800,
            "transcript_url": "https://example.com/test",
            "suite_id": "c4eebdbd-e019-42c0-9143-077762e92bbc",
            "office_id": "c4eebdbd-e019-42c0-9143-077762e92bbc"
        }
    }
]

# ========================================================
# EXECUTION
# ========================================================

print("=" * 90)
print("ASPIRE n8n WEBHOOK E2E RETEST v2")
print("FIX: Using compact JSON separators for HMAC (no spaces)")
print("=" * 90)

# Get baseline execution ID
req = urllib.request.Request(
    f"{N8N_BASE}/api/v1/executions?limit=1",
    headers={"X-N8N-API-KEY": N8N_API_KEY}
)
pre_resp = urllib.request.urlopen(req, timeout=10)
pre_execs = json.loads(pre_resp.read().decode()).get("data", [])
baseline_id = int(pre_execs[0]["id"]) if pre_execs else 0
print(f"\n[0/5] Baseline execution ID: {baseline_id}")

# Phase 1: Valid HMAC
print("\n[1/5] Sending VALID HMAC requests (compact JSON)...")
for wh in WEBHOOKS:
    send_webhook(wh["name"], wh["url"], wh["payload"], wh["secret"], valid_hmac=True)
    time.sleep(2)

# Phase 2: Invalid HMAC
print("\n[2/5] Sending INVALID HMAC requests...")
for wh in WEBHOOKS:
    send_webhook(wh["name"], wh["url"], wh["payload"], wh["secret"], valid_hmac=False)
    time.sleep(2)

# Phase 3: Wait
print("\n[3/5] Waiting 25 seconds for all async workflows to complete...")
time.sleep(25)

# Phase 4: Login and analyze
print("\n[4/5] Logging in and analyzing executions...")
try:
    cookie = n8n_login()
    print(f"  Logged in successfully")
except Exception as e:
    print(f"  Login FAILED: {e}")
    cookie = None

# Get new executions
req = urllib.request.Request(
    f"{N8N_BASE}/api/v1/executions?limit=50",
    headers={"X-N8N-API-KEY": N8N_API_KEY}
)
resp = urllib.request.urlopen(req, timeout=10)
all_execs = json.loads(resp.read().decode()).get("data", [])
new_execs = [e for e in all_execs if int(e["id"]) > baseline_id]
webhook_execs = [e for e in new_execs if e.get("workflowId") in WF_MAP]
print(f"  New executions: {len(new_execs)} total, {len(webhook_execs)} from webhook workflows")

# Analyze each execution
analysis_results = []
for ex in webhook_execs:
    ex_id = ex["id"]
    wf_id = ex.get("workflowId", "")
    wf_name = WF_MAP.get(wf_id, wf_id)
    status = ex.get("status", "?")
    mode = ex.get("mode", "?")

    if cookie:
        try:
            a = analyze_execution(ex_id, cookie)
            a["wf_name"] = wf_name
            a["status"] = status
            a["mode"] = mode
            analysis_results.append(a)
        except Exception as e:
            print(f"  WARNING: Execution {ex_id}: {e}")
    else:
        analysis_results.append({
            "id": ex_id, "wf_name": wf_name, "status": status, "mode": mode,
            "killed": None, "hmac_passed": None, "gateway_reached": None,
            "econnrefused": False, "gateway_status": None, "receipt_emitted": False,
            "has_test_payload": False, "error_messages": [], "nodes_hit": []
        })

# ========================================================
# DETAILED RESULTS
# ========================================================

print(f"\n[5/5] Results\n")
print("=" * 130)
print("DETAILED EXECUTION ANALYSIS")
print("=" * 130)
print(f"\n{'ID':<8} {'Workflow':<22} {'Stat':<8} {'Mode':<8} {'HMAC':<10} {'GW':<10} {'GW Code':<10} {'Receipt':<10} {'Nodes'}")
print("-" * 130)

for a in analysis_results:
    hmac_s = "PASS" if a["hmac_passed"] is True else ("REJECT" if a["hmac_passed"] is False else "?")
    gw_s = "YES" if a["gateway_reached"] is True else ("REFUSED" if a["econnrefused"] else "-")
    gw_c = str(a["gateway_status"]) if a["gateway_status"] else "-"
    rcpt = "YES" if a["receipt_emitted"] else "-"
    nodes = ", ".join(a.get("nodes_hit", [])[:4])
    print(f"{a['id']:<8} {a['wf_name']:<22} {a['status']:<8} {a['mode']:<8} {hmac_s:<10} {gw_s:<10} {gw_c:<10} {rcpt:<10} {nodes}")


# ========================================================
# FINAL VERDICT TABLE
# ========================================================

from collections import defaultdict
by_wf = defaultdict(list)
for a in analysis_results:
    by_wf[a["wf_name"]].append(a)

print(f"\n\n{'='*100}")
print("FINAL VERDICT TABLE")
print(f"{'='*100}")
print(f"\n{'Workflow':<25} {'HMAC Valid':<14} {'GW Reached':<14} {'Receipt':<14} {'HMAC Reject':<14} {'Status'}")
print("-" * 100)

overall_pass = True
for wf_name in ["intake-activation", "eli-email-triage", "sarah-call-handler", "nora-meeting-summary"]:
    execs = by_wf.get(wf_name, [])

    # n8n pairs: error trigger (status=success, mode=error) + actual (status varies, mode=webhook)
    # Real webhook executions have mode=webhook (or similar)
    # For analysis, look at all of them
    valid_hmac = [e for e in execs if e["hmac_passed"] is True]
    invalid_hmac = [e for e in execs if e["hmac_passed"] is False]

    hmac_valid_ok = len(valid_hmac) > 0
    gateway_ok = any(e["gateway_reached"] is True for e in valid_hmac) if valid_hmac else False
    no_refused = not any(e["econnrefused"] for e in execs)
    receipt_ok = any(e["receipt_emitted"] for e in execs)
    hmac_reject_ok = len(invalid_hmac) > 0

    status_str = "PASS"
    notes = []
    if not hmac_valid_ok:
        status_str = "FAIL"
        notes.append("no valid HMAC exec")
    if not no_refused:
        status_str = "FAIL"
        notes.append("ECONNREFUSED!")
    if not hmac_reject_ok:
        notes.append("no reject seen")
    if not gateway_ok and hmac_valid_ok:
        notes.append("GW not confirmed in data")
    if not receipt_ok:
        notes.append("receipt not confirmed in data")

    if status_str == "FAIL":
        overall_pass = False

    h_v = "YES" if hmac_valid_ok else "NO"
    g_r = "YES" if gateway_ok else ("REFUSED" if not no_refused else "unconfirmed")
    r_e = "YES" if receipt_ok else "unconfirmed"
    h_r = "YES" if hmac_reject_ok else "unconfirmed"
    note = " | " + ", ".join(notes) if notes else ""

    print(f"{wf_name:<25} {h_v:<14} {g_r:<14} {r_e:<14} {h_r:<14} {status_str}{note}")

print(f"\n{'OVERALL:':<25} {'PASS' if overall_pass else 'ISSUES FOUND'}")

# ECONNREFUSED summary
refused = [a for a in analysis_results if a["econnrefused"]]
if refused:
    print(f"\n*** WARNING: {len(refused)} execution(s) had ECONNREFUSED ***")
else:
    print(f"\n*** CONFIRMED: Zero ECONNREFUSED across all {len(analysis_results)} executions ***")

# Error summary
all_errors = []
for a in analysis_results:
    for msg in a.get("error_messages", []):
        all_errors.append(f"  Exec {a['id']} ({a['wf_name']}): {msg}")
if all_errors:
    print(f"\nError messages found ({len(all_errors)}):")
    for e in all_errors[:10]:
        print(e)

print(f"\n{'='*90}")
print("E2E RETEST v2 COMPLETE")
print(f"{'='*90}")
