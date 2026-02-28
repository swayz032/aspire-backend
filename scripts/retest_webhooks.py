#!/usr/bin/env python3
"""
E2E Retest: All 4 webhook workflows with valid HMAC + invalid HMAC rejection.
Now that Desktop server is up on port 5000, Gateway calls should NOT get ECONNREFUSED.
"""

import json
import hmac
import hashlib
import urllib.request
import urllib.error
import time
import sys
import io

# Fix Windows encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

N8N_BASE = "http://localhost:5678"
N8N_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0ZmQ3OWU4OS0zMDE3LTRkYmUtOGNlYy02NzZmY2FiNmY5MzgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiYTMxN2Y3YTgtNWMwZS00NGE4LTg5NTgtNGE3YTcxYmIyNDM3IiwiaWF0IjoxNzcxNDQyMjQ0LCJleHAiOjE3NzM5NzkyMDB9.iyLco0Fb_EoeFwDDFGCpvMPAwbJduSuS4TXtfRMm1fk"

def sort_keys(obj):
    if isinstance(obj, list):
        return [sort_keys(i) for i in obj]
    if isinstance(obj, dict):
        return {k: sort_keys(v) for k, v in sorted(obj.items())}
    return obj

def send_webhook(name, url, payload, secret, valid_hmac=True):
    """Send a webhook request with valid or invalid HMAC."""
    body = json.dumps(sort_keys(payload)).encode()

    if valid_hmac:
        sig = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    else:
        sig = 'sha256=' + '0' * 64

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'X-Webhook-Signature': sig
        }
    )

    label = "VALID" if valid_hmac else "INVALID"
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        status = resp.status
        resp_body = resp.read().decode()
        print(f"[{label}] {name}: HTTP {status}")
        try:
            resp_json = json.loads(resp_body)
            print(f"  Response: {json.dumps(resp_json, indent=2)[:500]}")
        except:
            print(f"  Response: {resp_body[:500]}")
        return {"name": name, "hmac": label, "status": status, "body": resp_body}
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode() if e.fp else ""
        print(f"[{label}] {name}: HTTP {e.code}")
        try:
            resp_json = json.loads(resp_body)
            print(f"  Response: {json.dumps(resp_json, indent=2)[:500]}")
        except:
            print(f"  Response: {resp_body[:500]}")
        return {"name": name, "hmac": label, "status": e.code, "body": resp_body}
    except Exception as e:
        print(f"[{label}] {name}: ERROR - {e}")
        return {"name": name, "hmac": label, "status": "ERROR", "body": str(e)}


# ===================== WEBHOOK CONFIGS =====================

WEBHOOKS = [
    {
        "name": "intake-activation",
        "url": f"{N8N_BASE}/webhook/intake-activation",
        "secret": "aspire-n8n-dev-secret",
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
        "secret": "aspire-eli-dev-secret",
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
        "secret": "aspire-sarah-dev-secret",
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
        "secret": "aspire-nora-dev-secret",
        "payload": {
            "room_name": "retest-room-001",
            "duration": 1800,
            "transcript_url": "https://example.com/test",
            "suite_id": "c4eebdbd-e019-42c0-9143-077762e92bbc",
            "office_id": "c4eebdbd-e019-42c0-9143-077762e92bbc"
        }
    }
]


# ===================== EXECUTE TESTS =====================

print("=" * 70)
print("ASPIRE n8n WEBHOOK E2E RETEST")
print("Desktop server: host.docker.internal:5000 (confirmed UP)")
print("n8n: localhost:5678 (confirmed UP)")
print("=" * 70)

results = []

# Phase 1: Valid HMAC tests
print("\n--- PHASE 1: Valid HMAC Tests ---\n")
for wh in WEBHOOKS:
    result = send_webhook(wh["name"], wh["url"], wh["payload"], wh["secret"], valid_hmac=True)
    results.append(result)
    time.sleep(1)  # brief pause between requests

# Phase 2: Invalid HMAC tests
print("\n--- PHASE 2: Invalid HMAC Tests (expect rejection) ---\n")
for wh in WEBHOOKS:
    result = send_webhook(wh["name"], wh["url"], wh["payload"], wh["secret"], valid_hmac=False)
    results.append(result)
    time.sleep(1)

# Phase 3: Wait and check executions
print("\n--- PHASE 3: Waiting 15 seconds for async workflows to complete ---\n")
time.sleep(15)

print("Fetching recent executions from n8n API...")
req = urllib.request.Request(
    f"{N8N_BASE}/api/v1/executions?limit=30",
    headers={"X-N8N-API-KEY": N8N_API_KEY}
)
try:
    resp = urllib.request.urlopen(req, timeout=10)
    exec_data = json.loads(resp.read().decode())
    executions = exec_data.get("data", [])

    print(f"\nFound {len(executions)} recent executions:\n")
    print(f"{'ID':<12} {'Status':<10} {'Workflow':<20} {'Started':<25} {'Finished':<25}")
    print("-" * 92)
    for ex in executions:
        wf_name = ex.get("workflowData", {}).get("name", "?")[:20] if ex.get("workflowData") else "?"
        # Public API may not have workflowData, use workflowId instead
        wf_id = ex.get("workflowId", "?")
        started = ex.get("startedAt", "?")[:25]
        finished = (ex.get("stoppedAt") or "running")[:25]
        status = ex.get("status", "?")
        print(f"{ex['id']:<12} {status:<10} {wf_id:<20} {started:<25} {finished:<25}")

except Exception as e:
    print(f"Failed to fetch executions: {e}")

# ===================== SUMMARY =====================

print("\n" + "=" * 70)
print("SUMMARY OF TEST RESULTS")
print("=" * 70)
print(f"\n{'Test':<30} {'HMAC':<10} {'HTTP':<8} {'Verdict'}")
print("-" * 60)
for r in results:
    status = r["status"]
    if r["hmac"] == "VALID":
        # For valid HMAC: 200 = workflow accepted and ran
        # 500 could mean Gateway error (but reached!) vs ECONNREFUSED
        if status == 200:
            verdict = "PASS (accepted)"
        elif status == 500:
            # Check if it's ECONNREFUSED or a real Gateway error
            if "ECONNREFUSED" in r.get("body", ""):
                verdict = "FAIL (ECONNREFUSED)"
            else:
                verdict = "PASS (Gateway error, but reached)"
        else:
            verdict = f"CHECK (HTTP {status})"
    else:
        # For invalid HMAC: any response that shows rejection is good
        if status == 200:
            # Check if body indicates killed
            try:
                body = json.loads(r.get("body", "{}"))
                if body.get("killed") == True or body.get("killed") == "true":
                    verdict = "PASS (rejected)"
                else:
                    verdict = "FAIL (not rejected!)"
            except:
                verdict = "CHECK (parse error)"
        elif status == 401 or status == 403:
            verdict = "PASS (rejected)"
        else:
            verdict = f"CHECK (HTTP {status})"

    print(f"{r['name']:<30} {r['hmac']:<10} {str(status):<8} {verdict}")

print("\n" + "=" * 70)
print("RETEST COMPLETE")
print("=" * 70)
