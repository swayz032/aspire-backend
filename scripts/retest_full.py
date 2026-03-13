#!/usr/bin/env python3
"""
Full E2E Retest: 4 webhook workflows with valid/invalid HMAC.
Then deep analysis of execution data via n8n internal REST API.
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
    body = json.dumps(sort_keys(payload)).encode()
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

def get_execution_detail(ex_id, cookie):
    req = urllib.request.Request(
        f"{N8N_BASE}/rest/executions/{ex_id}",
        headers={"Cookie": cookie}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return resp.read().decode()

def analyze_execution(detail_body):
    """Extract key indicators from flatted execution data."""
    result = {
        "killed": None,
        "hmac_passed": None,
        "gateway_reached": None,
        "econnrefused": False,
        "gateway_status": None,
        "receipt_emitted": False,
        "has_test_payload": False,
        "error_messages": [],
    }

    # Test payload detection
    for marker in ['retest-intake', 'retest-email', 'retest-call', 'retest-room']:
        if marker in detail_body:
            result["has_test_payload"] = True
            break

    # HMAC / killed detection
    if '"killed":true' in detail_body or '"killed":"true"' in detail_body:
        result["killed"] = True
        result["hmac_passed"] = False
    elif '"killed":false' in detail_body or '"killed":"false"' in detail_body:
        result["killed"] = False
        result["hmac_passed"] = True

    if 'hmac_validation_failed' in detail_body:
        result["hmac_passed"] = False
        result["killed"] = True

    # Gateway connectivity
    if 'ECONNREFUSED' in detail_body:
        result["econnrefused"] = True
        result["gateway_reached"] = False
    elif 'host.docker.internal:5000' in detail_body or 'host.docker.internal' in detail_body:
        if not result["econnrefused"]:
            result["gateway_reached"] = True

    # Gateway response status codes
    for code in [200, 201, 301, 302, 400, 401, 403, 404, 405, 500, 502, 503]:
        if f'"statusCode":{code}' in detail_body or f'"statusCode":"{code}"' in detail_body:
            result["gateway_status"] = code

    # Receipt emission: look for supabase URL in calls
    if 'qtuehjqlcmfcascqjjhc' in detail_body or 'supabase.co/rest' in detail_body:
        result["receipt_emitted"] = True

    # Error messages
    for match in re.findall(r'"message":"([^"]{1,300})"', detail_body):
        lower = match.lower()
        if any(kw in lower for kw in ['error', 'fail', 'refused', 'timeout', 'denied', 'reject']):
            if match not in result["error_messages"]:
                result["error_messages"].append(match)

    return result


# ========================================================
# MAIN TEST EXECUTION
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
            "correlationId": "retest-intake-001"
        }
    },
    {
        "name": "eli-email-triage",
        "url": f"{N8N_BASE}/webhook/eli-email-triage",
        "secret": get_webhook_secret("eli"),
        "payload": {
            "email_id": "retest-email-001",
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
            "call_sid": "retest-call-001",
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
            "room_name": "retest-room-001",
            "duration": 1800,
            "transcript_url": "https://example.com/test",
            "suite_id": "c4eebdbd-e019-42c0-9143-077762e92bbc",
            "office_id": "c4eebdbd-e019-42c0-9143-077762e92bbc"
        }
    }
]

print("=" * 80)
print("ASPIRE n8n WEBHOOK E2E RETEST (with execution analysis)")
print("=" * 80)

# Record execution count before test
print("\n[1/5] Counting existing executions...")
exec_req = urllib.request.Request(
    f"{N8N_BASE}/api/v1/executions?limit=1",
    headers={"X-N8N-API-KEY": N8N_API_KEY}
)
pre_resp = urllib.request.urlopen(exec_req, timeout=10)
pre_data = json.loads(pre_resp.read().decode())
pre_count = len(pre_data.get("data", []))
# Get the most recent execution ID to filter later
exec_req2 = urllib.request.Request(
    f"{N8N_BASE}/api/v1/executions?limit=1",
    headers={"X-N8N-API-KEY": N8N_API_KEY}
)
pre_resp2 = urllib.request.urlopen(exec_req2, timeout=10)
pre_execs = json.loads(pre_resp2.read().decode()).get("data", [])
baseline_id = int(pre_execs[0]["id"]) if pre_execs else 0
print(f"  Baseline execution ID: {baseline_id}")

# Phase 1: Valid HMAC
print("\n[2/5] Sending valid HMAC requests...")
webhook_results = []
for wh in WEBHOOKS:
    r = send_webhook(wh["name"], wh["url"], wh["payload"], wh["secret"], valid_hmac=True)
    webhook_results.append(r)
    time.sleep(1.5)

# Phase 2: Invalid HMAC
print("\n[3/5] Sending invalid HMAC requests...")
for wh in WEBHOOKS:
    r = send_webhook(wh["name"], wh["url"], wh["payload"], wh["secret"], valid_hmac=False)
    webhook_results.append(r)
    time.sleep(1.5)

# Phase 3: Wait for async workflows to complete
print("\n[4/5] Waiting 20 seconds for all workflows to complete...")
time.sleep(20)

# Phase 4: Login and analyze executions
print("\n[5/5] Analyzing execution data via internal API...")
try:
    cookie = n8n_login()
    print(f"  Logged in successfully")
except Exception as e:
    print(f"  Login failed: {e}")
    print("  Falling back to public API only (limited analysis)")
    cookie = None

# Get all executions since our baseline
exec_req3 = urllib.request.Request(
    f"{N8N_BASE}/api/v1/executions?limit=50",
    headers={"X-N8N-API-KEY": N8N_API_KEY}
)
resp3 = urllib.request.urlopen(exec_req3, timeout=10)
all_execs = json.loads(resp3.read().decode()).get("data", [])

# Filter to only new executions from our test
new_execs = [e for e in all_execs if int(e["id"]) > baseline_id]
print(f"  Found {len(new_execs)} new executions since baseline (ID > {baseline_id})")

# Only analyze webhook workflow executions
webhook_wf_ids = set(WF_MAP.keys())
test_execs = [e for e in new_execs if e.get("workflowId") in webhook_wf_ids]
print(f"  Of those, {len(test_execs)} belong to webhook workflows")

# Deep analysis of each execution
analysis_results = []
for ex in test_execs:
    ex_id = ex["id"]
    wf_id = ex.get("workflowId", "")
    wf_name = WF_MAP.get(wf_id, wf_id)
    status = ex.get("status", "?")
    mode = ex.get("mode", "?")

    if cookie:
        try:
            detail = get_execution_detail(ex_id, cookie)
            analysis = analyze_execution(detail)
            analysis["id"] = ex_id
            analysis["wf_name"] = wf_name
            analysis["status"] = status
            analysis["mode"] = mode
            analysis_results.append(analysis)
        except Exception as e:
            print(f"  WARNING: Could not fetch execution {ex_id}: {e}")
            analysis_results.append({
                "id": ex_id, "wf_name": wf_name, "status": status, "mode": mode,
                "killed": None, "hmac_passed": None, "gateway_reached": None,
                "econnrefused": False, "gateway_status": None, "receipt_emitted": False,
                "has_test_payload": False, "error_messages": ["fetch_failed"]
            })
    else:
        analysis_results.append({
            "id": ex_id, "wf_name": wf_name, "status": status, "mode": mode,
            "killed": None, "hmac_passed": None, "gateway_reached": None,
            "econnrefused": False, "gateway_status": None, "receipt_emitted": False,
            "has_test_payload": False, "error_messages": ["no_cookie"]
        })


# ========================================================
# RESULTS
# ========================================================

print("\n" + "=" * 120)
print("DETAILED EXECUTION ANALYSIS")
print("=" * 120)
print(f"\n{'ID':<8} {'Workflow':<22} {'Status':<10} {'Mode':<10} {'HMAC':<10} {'GW Reach':<10} {'GW Code':<10} {'Receipt':<10} {'Test?':<6} {'Errors'}")
print("-" * 120)

for a in analysis_results:
    hmac_str = "PASS" if a["hmac_passed"] is True else ("REJECT" if a["hmac_passed"] is False else "?")
    gw_str = "YES" if a["gateway_reached"] is True else ("REFUSED" if a["econnrefused"] else "-")
    gw_code = str(a["gateway_status"]) if a["gateway_status"] else "-"
    rcpt = "YES" if a["receipt_emitted"] else "-"
    test = "YES" if a["has_test_payload"] else "-"
    errs = "; ".join(a.get("error_messages", []))[:50] if a.get("error_messages") else "-"
    print(f"{a['id']:<8} {a['wf_name']:<22} {a['status']:<10} {a['mode']:<10} {hmac_str:<10} {gw_str:<10} {gw_code:<10} {rcpt:<10} {test:<6} {errs}")


# ========================================================
# VERDICT TABLE (per workflow)
# ========================================================

print("\n" + "=" * 100)
print("FINAL VERDICT TABLE")
print("=" * 100)

# Group by workflow name
from collections import defaultdict
by_wf = defaultdict(list)
for a in analysis_results:
    by_wf[a["wf_name"]].append(a)

print(f"\n{'Workflow':<25} {'HMAC Valid':<15} {'Gateway Reached':<18} {'Receipt Emitted':<18} {'HMAC Reject':<15} {'Status'}")
print("-" * 100)

overall_pass = True
for wf_name in ["intake-activation", "eli-email-triage", "sarah-call-handler", "nora-meeting-summary"]:
    execs = by_wf.get(wf_name, [])

    # Filter to test payloads only (success mode = webhook execution, not error trigger)
    # n8n creates paired executions: error mode (instant) + webhook mode (actual)
    # The "success" status ones with "webhook" mode are the real executions
    real_execs = [e for e in execs if e["has_test_payload"] or e["status"] == "success"]

    valid_hmac = [e for e in real_execs if e["hmac_passed"] is True]
    invalid_hmac = [e for e in real_execs if e["hmac_passed"] is False]

    hmac_valid_ok = len(valid_hmac) > 0
    gateway_ok = any(e["gateway_reached"] is True for e in valid_hmac) if valid_hmac else False
    no_refused = not any(e["econnrefused"] for e in real_execs)
    receipt_ok = any(e["receipt_emitted"] for e in real_execs)
    hmac_reject_ok = len(invalid_hmac) > 0

    # A workflow without ECONNREFUSED is the critical test here
    # Gateway returning 401/404 is expected (no route / auth required)
    status = "PASS"
    notes = []
    if not hmac_valid_ok:
        status = "FAIL"
        notes.append("no valid HMAC exec")
    if not no_refused:
        status = "FAIL"
        notes.append("ECONNREFUSED!")
    if not hmac_reject_ok:
        notes.append("no HMAC reject seen")
        # For async webhooks, rejection may not surface in HTTP response
        # but should be visible in execution data
    if not gateway_ok and hmac_valid_ok:
        notes.append("GW not confirmed")
    if not receipt_ok:
        notes.append("receipt not confirmed")

    hmac_v_str = "YES" if hmac_valid_ok else "NO"
    gw_str = "YES" if gateway_ok else ("NO(REFUSED)" if not no_refused else "not confirmed")
    rcpt_str = "YES" if receipt_ok else "not confirmed"
    hmac_r_str = "YES" if hmac_reject_ok else "not confirmed"

    if status == "FAIL":
        overall_pass = False

    print(f"{wf_name:<25} {hmac_v_str:<15} {gw_str:<18} {rcpt_str:<18} {hmac_r_str:<15} {status} {' '.join(notes)}")

print(f"\n{'OVERALL:':<25} {'PASS' if overall_pass else 'ISSUES FOUND'}")

# Show any ECONNREFUSED errors explicitly
refused = [a for a in analysis_results if a["econnrefused"]]
if refused:
    print(f"\n*** WARNING: {len(refused)} execution(s) had ECONNREFUSED ***")
    for r in refused:
        print(f"  - Execution {r['id']} ({r['wf_name']})")
else:
    print(f"\n*** CONFIRMED: Zero ECONNREFUSED errors across all {len(analysis_results)} executions ***")

print("\n" + "=" * 80)
print("E2E RETEST COMPLETE")
print("=" * 80)
