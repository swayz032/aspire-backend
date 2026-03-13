#!/usr/bin/env python3
"""Deep inspect execution 87 (valid HMAC intake) to understand HMAC comparison."""
import json
import urllib.request
import sys
import io
import re

from _n8n_runtime import get_n8n_admin_email, get_n8n_admin_password, get_n8n_base_url

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

N8N_BASE = get_n8n_base_url()

# Login
data = json.dumps({
    'emailOrLdapLoginId': get_n8n_admin_email(),
    'password': get_n8n_admin_password(),
}).encode()
req = urllib.request.Request(f"{N8N_BASE}/rest/login", data=data, headers={'Content-Type': 'application/json'})
resp = urllib.request.urlopen(req, timeout=10)
cookie = None
for c in (resp.headers.get_all("Set-Cookie") or []):
    if "n8n-auth" in c:
        cookie = c.split(";")[0]
        break

# Fetch execution 87 (valid HMAC intake)
req = urllib.request.Request(f"{N8N_BASE}/rest/executions/87", headers={"Cookie": cookie})
resp = urllib.request.urlopen(req, timeout=10)
body = resp.read().decode()

# Parse the flatted format to find the important bits
# Flatted format: first element is an array, containing indexed strings/objects

# Let's find the actual signature and killed output
# Look for patterns around killed/reason
print("=== Searching for key patterns in execution 87 ===\n")

# Find the killed:true and surrounding context
patterns = [
    (r'"killed":\s*(true|false)', 'killed value'),
    (r'"reason":\s*"([^"]*)"', 'reason'),
    (r'sha256=[a-f0-9]{64}', 'HMAC signature'),
    (r'x-webhook-signature', 'x-webhook-signature header key'),
    (r'N8N_WEBHOOK_SECRET', 'webhook secret env var reference'),
    (r'hmac_validation_failed', 'HMAC validation failed'),
    (r'kill_switch_disabled', 'kill switch disabled'),
    (r'missing_tenant_context', 'missing tenant context'),
]

for pattern, desc in patterns:
    matches = list(re.finditer(pattern, body))
    if matches:
        for m in matches:
            start = max(0, m.start() - 60)
            end = min(len(body), m.end() + 60)
            ctx = body[start:end].replace('\n', ' ')
            print(f"[{desc}] at pos {m.start()}: ...{ctx}...")
    else:
        print(f"[{desc}] NOT FOUND")
    print()

# Now try to decode the flatted format
# Flatted is a JSON array where the first element is the root
# and other elements are referenced by index
print("\n=== Attempting flatted decode ===\n")
try:
    parsed = json.loads(body)
    data = parsed.get("data", parsed)

    # Check if data has executionData
    if isinstance(data, dict):
        exec_data = data.get("data", {})
        if isinstance(exec_data, dict):
            result_data = exec_data.get("resultData", {})
            if isinstance(result_data, dict):
                run_data = result_data.get("runData", {})
                if isinstance(run_data, dict):
                    for node_name, node_data in run_data.items():
                        print(f"\nNode: {node_name}")
                        if isinstance(node_data, list) and len(node_data) > 0:
                            first = node_data[0]
                            if isinstance(first, dict):
                                out_data = first.get("data", {})
                                if isinstance(out_data, dict):
                                    main = out_data.get("main", [])
                                    if isinstance(main, list) and len(main) > 0:
                                        items = main[0]
                                        if isinstance(items, list):
                                            for item in items[:2]:
                                                if isinstance(item, dict):
                                                    j = item.get("json", {})
                                                    # Print key fields
                                                    print(f"  json keys: {list(j.keys())[:15]}")
                                                    if "killed" in j:
                                                        print(f"  killed: {j['killed']}")
                                                    if "reason" in j:
                                                        print(f"  reason: {j['reason']}")
                                                    if "headers" in j:
                                                        h = j["headers"]
                                                        if isinstance(h, dict):
                                                            sig = h.get("x-webhook-signature", "MISSING")
                                                            print(f"  x-webhook-signature: {sig[:80]}")
                                                    if "body" in j:
                                                        b = j["body"]
                                                        if isinstance(b, dict):
                                                            print(f"  body keys: {list(b.keys())}")
except Exception as e:
    print(f"Flatted decode failed (expected for flatted format): {e}")
    print("The data is in flatted format which requires special parsing")
