#!/usr/bin/env python3
import urllib.request, json, sys, io

from _n8n_runtime import get_n8n_api_key, get_n8n_base_url

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

N8N_BASE = get_n8n_base_url()
N8N_API_KEY = get_n8n_api_key()

req = urllib.request.Request(
    f"{N8N_BASE}/api/v1/workflows",
    headers={"X-N8N-API-KEY": N8N_API_KEY}
)
resp = urllib.request.urlopen(req, timeout=10)
data = json.loads(resp.read().decode())

webhook_ids = ["aDeQEKoBawhGdUtE", "s1JqxMYrEDbPVbMT", "Mf7SSxqkCQLflZt3", "6PXnv0hogAIbKG8F"]
for w in data.get("data", []):
    if w["id"] in webhook_ids:
        name = w["name"][:40]
        active = w["active"]
        wid = w["id"]
        print(f"{wid} | {name} | active={active}")
