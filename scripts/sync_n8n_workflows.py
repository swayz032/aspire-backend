"""
Sync local n8n workflow JSON files to running n8n instance.
Updates existing workflows and creates new ones.

Usage: python scripts/sync_n8n_workflows.py
"""
import json
import os
import sys
import urllib.request
import urllib.error

N8N_URL = os.environ.get("N8N_API_URL", "http://localhost:5678")
N8N_KEY = os.environ.get("N8N_API_KEY", "")

WORKFLOWS_DIR = os.path.join(os.path.dirname(__file__), "..", "infrastructure", "n8n-workflows")

# Map local filename (without .json) -> n8n workflow ID
EXISTING_MAP = {
    "adam-daily-brief": "b4PyuAornfgtUrke",
    "adam-education-curate": "yNVifQzIzJHkpeY2",
    "adam-focus-weekly": "oqKt8y3qnVLfYFFP",
    "adam-library-curate": "crxdfUDxki7PWeFz",
    "adam-pulse-scan": "NwnvnWBbYV3TlDnz",
    "eli-email-triage": "s1JqxMYrEDbPVbMT",
    "intake-activation": "aDeQEKoBawhGdUtE",
    "nora-meeting-summary": "6PXnv0hogAIbKG8F",
    "quinn-invoice-reminder": "AwRm4u3aRZdBJC9E",
    "sarah-call-handler": "Mf7SSxqkCQLflZt3",
    "teressa-books-sync": "5zkLtqPi5Qn2pedH",
}

def api_request(method, path, data=None):
    """Make authenticated n8n API request."""
    url = f"{N8N_URL}/api/v1{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-N8N-API-KEY", N8N_KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": body, "status": e.code}, e.code

def update_workflow(wf_id, local_data):
    """Update an existing workflow via PUT."""
    payload = {
        "name": local_data.get("name", ""),
        "nodes": local_data.get("nodes", []),
        "connections": local_data.get("connections", {}),
        "settings": local_data.get("settings", {}),
    }
    return api_request("PUT", f"/workflows/{wf_id}", payload)

def create_workflow(local_data):
    """Create a new workflow via POST."""
    payload = {
        "name": local_data.get("name", "Unnamed"),
        "nodes": local_data.get("nodes", []),
        "connections": local_data.get("connections", {}),
        "settings": local_data.get("settings", {}),
    }
    return api_request("POST", "/workflows", payload)

def main():
    if not N8N_KEY:
        print("ERROR: N8N_API_KEY not set")
        sys.exit(1)

    # Test connection
    resp, status = api_request("GET", "/workflows?limit=1")
    if status != 200:
        print(f"ERROR: Cannot connect to n8n at {N8N_URL}: {resp}")
        sys.exit(1)
    print(f"Connected to n8n at {N8N_URL}\n")

    results = {"updated": [], "created": [], "failed": []}

    # Process all JSON files in workflows dir
    for filename in sorted(os.listdir(WORKFLOWS_DIR)):
        if not filename.endswith(".json"):
            continue
        name = filename.replace(".json", "")
        filepath = os.path.join(WORKFLOWS_DIR, filename)

        with open(filepath, "r", encoding="utf-8") as f:
            local_data = json.load(f)

        if name in EXISTING_MAP:
            wf_id = EXISTING_MAP[name]
            print(f"  UPDATE {filename} -> {wf_id} ... ", end="", flush=True)
            resp, status = update_workflow(wf_id, local_data)
            if status == 200:
                print("OK")
                results["updated"].append(name)
            else:
                print(f"FAILED ({status})")
                results["failed"].append((name, status, resp))
        else:
            print(f"  CREATE {filename} ... ", end="", flush=True)
            resp, status = create_workflow(local_data)
            if status in (200, 201):
                new_id = resp.get("id", "?")
                print(f"OK (id={new_id})")
                results["created"].append((name, new_id))
            else:
                print(f"FAILED ({status})")
                results["failed"].append((name, status, resp))

    # Summary
    print(f"\n{'='*50}")
    print(f"Updated: {len(results['updated'])}")
    print(f"Created: {len(results['created'])}")
    print(f"Failed:  {len(results['failed'])}")
    if results["failed"]:
        print("\nFailed details:")
        for name, status, resp in results["failed"]:
            print(f"  {name}: HTTP {status} - {json.dumps(resp)[:200]}")
    if results["created"]:
        print("\nNew workflow IDs (update EXISTING_MAP):")
        for name, wf_id in results["created"]:
            print(f'    "{name}": "{wf_id}",')

    sys.exit(1 if results["failed"] else 0)

if __name__ == "__main__":
    main()
