#!/usr/bin/env python3
import urllib.request, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

N8N_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0ZmQ3OWU4OS0zMDE3LTRkYmUtOGNlYy02NzZmY2FiNmY5MzgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiYTMxN2Y3YTgtNWMwZS00NGE4LTg5NTgtNGE3YTcxYmIyNDM3IiwiaWF0IjoxNzcxNDQyMjQ0LCJleHAiOjE3NzM5NzkyMDB9.iyLco0Fb_EoeFwDDFGCpvMPAwbJduSuS4TXtfRMm1fk"

req = urllib.request.Request(
    "http://localhost:5678/api/v1/workflows",
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
