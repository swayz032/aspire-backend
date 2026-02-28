#!/usr/bin/env python3
"""Inspect a single execution to understand HMAC behavior."""
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
req = urllib.request.Request(
    f"{N8N_BASE}/rest/login",
    data=data,
    headers={'Content-Type': 'application/json'}
)
resp = urllib.request.urlopen(req, timeout=10)
cookie = None
for c in (resp.headers.get_all("Set-Cookie") or []):
    if "n8n-auth" in c:
        cookie = c.split(";")[0]
        break

if not cookie:
    print("Login failed")
    sys.exit(1)

# Fetch execution 87 (intake-activation valid HMAC, webhook mode)
# and execution 95 (intake-activation invalid HMAC, webhook mode)
for exec_id in [87, 95]:
    print(f"\n{'='*80}")
    print(f"EXECUTION {exec_id}")
    print(f"{'='*80}")

    req = urllib.request.Request(
        f"{N8N_BASE}/rest/executions/{exec_id}",
        headers={"Cookie": cookie}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    body = resp.read().decode()

    # Search for killed/hmac patterns
    print(f"\nLength: {len(body)} chars")

    # Find 'killed' occurrences
    for kw in ['killed', 'hmac', 'HMAC', 'signature', 'Signature', 'ECONNREFUSED', 'retest-', 'host.docker.internal']:
        idx = body.lower().find(kw.lower())
        if idx >= 0:
            context = body[max(0, idx-100):idx+200]
            # Clean up for readability
            context = context.replace('\n', ' ').replace('\r', '')
            print(f"\n  [{kw}] found at pos {idx}:")
            print(f"    ...{context}...")
        else:
            print(f"\n  [{kw}] NOT FOUND")
