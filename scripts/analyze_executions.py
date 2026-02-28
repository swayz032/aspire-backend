#!/usr/bin/env python3
"""
Analyze recent n8n executions via internal REST API to verify:
1. HMAC pass/fail for each execution
2. Gateway connectivity (no ECONNREFUSED)
3. Receipt emission
"""

import json
import urllib.request
import urllib.error
import sys
import io
import re

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

N8N_BASE = "http://localhost:5678"

# Workflow ID -> name mapping
WF_MAP = {
    "aDeQEKoBawhGdUtE": "intake-activation",
    "s1JqxMYrEDbPVbMT": "eli-email-triage",
    "Mf7SSxqkCQLflZt3": "sarah-call-handler",
    "6PXnv0hogAIbKG8F": "nora-meeting-summary",
}

# Step 1: Login to get session cookie
print("Logging in to n8n internal API...")
login_data = json.dumps({
    "emailOrLdapLoginId": "admin@aspire.local",
    "password": "aspire-n8n-dev"
}).encode()

login_req = urllib.request.Request(
    f"{N8N_BASE}/rest/login",
    data=login_data,
    headers={"Content-Type": "application/json"}
)

try:
    login_resp = urllib.request.urlopen(login_req, timeout=10)
    cookies = login_resp.headers.get_all("Set-Cookie")
    session_cookie = None
    for c in (cookies or []):
        if "n8n-auth" in c:
            session_cookie = c.split(";")[0]
            break

    if not session_cookie:
        print("ERROR: No n8n-auth cookie received")
        sys.exit(1)

    print(f"Login successful. Cookie: {session_cookie[:30]}...")
except Exception as e:
    print(f"Login failed: {e}")
    sys.exit(1)

# Step 2: Get the most recent 20 executions
print("\nFetching recent executions...")
exec_req = urllib.request.Request(
    f"{N8N_BASE}/rest/executions?limit=20&orderBy=id&orderDirection=DESC",
    headers={"Cookie": session_cookie}
)

try:
    exec_resp = urllib.request.urlopen(exec_req, timeout=10)
    exec_body = exec_resp.read().decode()
    exec_data = json.loads(exec_body)

    # Handle different response shapes
    executions = exec_data.get("data", {}).get("results", [])
    if not executions:
        executions = exec_data.get("data", [])
    if not executions:
        executions = exec_data.get("results", [])

    print(f"Found {len(executions)} executions")

except Exception as e:
    print(f"Failed to fetch executions: {e}")
    sys.exit(1)

# Step 3: Analyze each execution that belongs to our webhook workflows
print("\n" + "=" * 90)
print("DETAILED EXECUTION ANALYSIS")
print("=" * 90)

# Filter to only our 4 webhook workflows and the most recent test window (last 16)
webhook_execs = []
for ex in executions:
    wf_id = ex.get("workflowId", "")
    if wf_id in WF_MAP:
        webhook_execs.append(ex)

print(f"\nFiltered to {len(webhook_execs)} webhook workflow executions")

# For each execution, fetch full details
results_by_workflow = {}
for ex in webhook_execs[:16]:  # Last 16 = 4 webhooks x 2 hmac tests x 2 (error trigger pair)
    ex_id = ex.get("id")
    wf_id = ex.get("workflowId", "")
    wf_name = WF_MAP.get(wf_id, wf_id)
    status = ex.get("status", "?")

    print(f"\n--- Execution {ex_id} ({wf_name}, status={status}) ---")

    detail_req = urllib.request.Request(
        f"{N8N_BASE}/rest/executions/{ex_id}",
        headers={"Cookie": session_cookie}
    )

    try:
        detail_resp = urllib.request.urlopen(detail_req, timeout=10)
        detail_body = detail_resp.read().decode()

        # The data is in flatted format, search for key strings
        # Check for HMAC validation result
        hmac_passed = None
        gateway_reached = None
        econnrefused = False
        receipt_emitted = None
        killed = None

        # Search for 'killed' indicators
        if '"killed":true' in detail_body or '"killed":"true"' in detail_body:
            killed = True
            hmac_passed = False
        elif '"killed":false' in detail_body or '"killed":"false"' in detail_body:
            killed = False
            hmac_passed = True

        # Search for HMAC validation
        if 'hmac_validation_failed' in detail_body:
            hmac_passed = False
            killed = True

        # Search for ECONNREFUSED
        if 'ECONNREFUSED' in detail_body:
            econnrefused = True
            gateway_reached = False
        elif 'host.docker.internal:5000' in detail_body or 'gateway' in detail_body.lower():
            gateway_reached = True

        # Search for receipt emission indicators
        if 'receipt' in detail_body.lower() and ('supabase' in detail_body.lower() or 'qtuehjqlcmfcascqjjhc' in detail_body):
            receipt_emitted = True

        # Check for Gateway HTTP response codes
        gateway_status = None
        for code in ['statusCode":200', 'statusCode":201', 'statusCode":401', 'statusCode":404', 'statusCode":500']:
            if code in detail_body:
                gateway_status = code.split(":")[1].strip('"')

        # Print findings
        print(f"  killed={killed}, hmac_passed={hmac_passed}")
        print(f"  gateway_reached={gateway_reached}, econnrefused={econnrefused}")
        print(f"  gateway_status={gateway_status}")
        print(f"  receipt_indicators={'yes' if receipt_emitted else 'checking...'}")

        # Look for specific node outputs
        # Search for key patterns
        if 'retest-intake' in detail_body or 'retest-email' in detail_body or 'retest-call' in detail_body or 'retest-room' in detail_body:
            print(f"  test_payload_found=True (this is from our retest)")
        else:
            print(f"  test_payload_found=False (may be from earlier test or error trigger)")

        # Extract error messages if present
        error_matches = re.findall(r'"message":"([^"]{1,200})"', detail_body)
        for msg in error_matches[:5]:
            if 'error' in msg.lower() or 'fail' in msg.lower() or 'refused' in msg.lower() or 'timeout' in msg.lower():
                print(f"  error_msg: {msg}")

        # Store result
        key = f"{wf_name}_{status}_{ex_id}"
        if wf_name not in results_by_workflow:
            results_by_workflow[wf_name] = []
        results_by_workflow[wf_name].append({
            "id": ex_id,
            "status": status,
            "killed": killed,
            "hmac_passed": hmac_passed,
            "gateway_reached": gateway_reached,
            "econnrefused": econnrefused,
            "gateway_status": gateway_status,
            "has_test_payload": 'retest-' in detail_body,
        })

    except Exception as e:
        print(f"  ERROR fetching details: {e}")


# Step 4: Summary Table
print("\n\n" + "=" * 100)
print("FINAL RESULTS TABLE")
print("=" * 100)
print(f"\n{'Workflow':<25} {'Exec ID':<10} {'Status':<10} {'HMAC':<12} {'GW Reached':<14} {'GW Status':<12} {'Test Payload'}")
print("-" * 100)

for wf_name in ["intake-activation", "eli-email-triage", "sarah-call-handler", "nora-meeting-summary"]:
    execs = results_by_workflow.get(wf_name, [])
    for ex in execs:
        hmac_str = "PASS" if ex["hmac_passed"] else ("REJECT" if ex["hmac_passed"] is False else "?")
        gw_str = "YES" if ex["gateway_reached"] else ("NO(REFUSED)" if ex["econnrefused"] else "N/A")
        gw_status = ex["gateway_status"] or "-"
        payload = "YES" if ex["has_test_payload"] else "no"
        print(f"{wf_name:<25} {str(ex['id']):<10} {ex['status']:<10} {hmac_str:<12} {gw_str:<14} {gw_status:<12} {payload}")


# Step 5: Verdicts
print("\n\n" + "=" * 100)
print("VERDICT SUMMARY")
print("=" * 100)

for wf_name in ["intake-activation", "eli-email-triage", "sarah-call-handler", "nora-meeting-summary"]:
    execs = results_by_workflow.get(wf_name, [])
    test_execs = [e for e in execs if e["has_test_payload"]]

    # Separate valid vs invalid HMAC tests
    valid_hmac_execs = [e for e in test_execs if e["hmac_passed"] is True]
    invalid_hmac_execs = [e for e in test_execs if e["hmac_passed"] is False]

    valid_ok = len(valid_hmac_execs) > 0
    invalid_ok = len(invalid_hmac_execs) > 0
    no_refused = not any(e["econnrefused"] for e in test_execs)

    status = "PASS" if (valid_ok and invalid_ok and no_refused) else "PARTIAL" if (valid_ok or invalid_ok) else "FAIL"
    print(f"\n{wf_name}:")
    print(f"  Valid HMAC accepted:    {'YES' if valid_ok else 'NO'} ({len(valid_hmac_execs)} executions)")
    print(f"  Invalid HMAC rejected:  {'YES' if invalid_ok else 'NO'} ({len(invalid_hmac_execs)} executions)")
    print(f"  No ECONNREFUSED:        {'YES' if no_refused else 'NO'}")
    print(f"  Overall: {status}")
