"""
Re-sync all 11 agent workflows from JSON files and activate them.
Reads API key from .mcp.json, syncs each workflow via PUT, then activates via POST.
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:5678"
API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0ZmQ3OWU4OS0zMDE3LTRkYmUtOGNlYy02NzZmY2FiNmY5MzgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiYTMxN2Y3YTgtNWMwZS00NGE4LTg5NTgtNGE3YTcxYmIyNDM3IiwiaWF0IjoxNzcxNDQyMjQ0LCJleHAiOjE3NzM5NzkyMDB9.iyLco0Fb_EoeFwDDFGCpvMPAwbJduSuS4TXtfRMm1fk"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(PROJECT_ROOT, "infrastructure", "n8n-workflows")

WORKFLOW_MAP = {
    "adam-daily-brief":       "b4PyuAornfgtUrke",
    "adam-education-curate":  "yNVifQzIzJHkpeY2",
    "adam-focus-weekly":      "oqKt8y3qnVLfYFFP",
    "adam-library-curate":    "crxdfUDxki7PWeFz",
    "adam-pulse-scan":        "NwnvnWBbYV3TlDnz",
    "eli-email-triage":       "s1JqxMYrEDbPVbMT",
    "intake-activation":      "aDeQEKoBawhGdUtE",
    "nora-meeting-summary":   "6PXnv0hogAIbKG8F",
    "quinn-invoice-reminder": "AwRm4u3aRZdBJC9E",
    "sarah-call-handler":     "Mf7SSxqkCQLflZt3",
    "teressa-books-sync":     "5zkLtqPi5Qn2pedH",
}


def api_request(method, path, body=None):
    """Make an API request to n8n."""
    url = BASE_URL + path
    headers = {
        "X-N8N-API-KEY": API_KEY,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"  HTTP {e.code}: {err_body[:300]}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None


def check_current_status():
    """Check which workflows are currently active."""
    print("=" * 70)
    print("STEP 1: Checking current workflow status")
    print("=" * 70)
    result = api_request("GET", "/api/v1/workflows?limit=25")
    if not result:
        print("FATAL: Cannot reach n8n API")
        sys.exit(1)

    active_count = 0
    inactive_count = 0
    found_ids = set()

    for wf in result.get("data", []):
        wf_id = wf["id"]
        found_ids.add(wf_id)
        active = wf.get("active", False)
        name = wf.get("name", "unknown")
        status = "ACTIVE" if active else "INACTIVE"
        if active:
            active_count += 1
        else:
            inactive_count += 1
        # Check if this is one of our 11
        is_ours = wf_id in WORKFLOW_MAP.values()
        marker = " <-- AGENT" if is_ours else ""
        print(f"  {wf_id}  {status:8s}  {name}{marker}")

    print(f"\nTotal: {active_count} active, {inactive_count} inactive")

    # Check for missing workflows
    our_ids = set(WORKFLOW_MAP.values())
    missing = our_ids - found_ids
    if missing:
        print(f"\nWARNING: {len(missing)} agent workflows not found in n8n:")
        for name, wid in WORKFLOW_MAP.items():
            if wid in missing:
                print(f"  {name}: {wid}")
    return found_ids


def sync_workflow(name, wf_id):
    """Sync a single workflow from JSON file to n8n via PUT."""
    json_path = os.path.join(WORKFLOWS_DIR, f"{name}.json")
    if not os.path.exists(json_path):
        print(f"  SKIP {name}: file not found at {json_path}")
        return False

    with open(json_path, "r", encoding="utf-8") as f:
        wf_data = json.load(f)

    # Build PUT payload with required fields
    payload = {
        "name": wf_data.get("name", name),
        "nodes": wf_data.get("nodes", []),
        "connections": wf_data.get("connections", {}),
        "settings": wf_data.get("settings", {}),
    }

    result = api_request("PUT", f"/api/v1/workflows/{wf_id}", payload)
    if result:
        print(f"  SYNCED  {name} ({wf_id})")
        return True
    else:
        print(f"  FAILED  {name} ({wf_id})")
        return False


def activate_workflow(name, wf_id):
    """Activate a single workflow via POST."""
    result = api_request("POST", f"/api/v1/workflows/{wf_id}/activate")
    if result:
        active = result.get("active", False)
        if active:
            print(f"  ACTIVATED  {name} ({wf_id})")
            return True
        else:
            print(f"  WARN: activate returned but active=false for {name}")
            return False
    else:
        print(f"  FAILED TO ACTIVATE  {name} ({wf_id})")
        return False


def verify_all_active():
    """Final verification that all 11 are active."""
    print("\n" + "=" * 70)
    print("STEP 4: Final verification")
    print("=" * 70)
    result = api_request("GET", "/api/v1/workflows?limit=25")
    if not result:
        print("FATAL: Cannot reach n8n API for verification")
        return False

    wf_status = {}
    for wf in result.get("data", []):
        wf_status[wf["id"]] = {
            "active": wf.get("active", False),
            "name": wf.get("name", "unknown"),
        }

    all_good = True
    for name, wf_id in sorted(WORKFLOW_MAP.items()):
        if wf_id in wf_status:
            active = wf_status[wf_id]["active"]
            n8n_name = wf_status[wf_id]["name"]
            status = "ACTIVE" if active else "INACTIVE"
            mark = "PASS" if active else "FAIL"
            print(f"  [{mark}] {name:25s} {wf_id}  {status}")
            if not active:
                all_good = False
        else:
            print(f"  [FAIL] {name:25s} {wf_id}  NOT FOUND")
            all_good = False

    return all_good


def main():
    print("n8n Workflow Re-Sync and Activate")
    print("=" * 70)

    # Step 1: Check current status
    found_ids = check_current_status()

    # Step 2: Sync all 11 workflows from JSON files
    print("\n" + "=" * 70)
    print("STEP 2: Syncing workflows from JSON files")
    print("=" * 70)
    sync_ok = 0
    sync_fail = 0
    for name, wf_id in sorted(WORKFLOW_MAP.items()):
        if sync_workflow(name, wf_id):
            sync_ok += 1
        else:
            sync_fail += 1
    print(f"\nSync results: {sync_ok} synced, {sync_fail} failed")

    # Step 3: Activate all 11 workflows
    print("\n" + "=" * 70)
    print("STEP 3: Activating all workflows")
    print("=" * 70)
    act_ok = 0
    act_fail = 0
    for name, wf_id in sorted(WORKFLOW_MAP.items()):
        if activate_workflow(name, wf_id):
            act_ok += 1
        else:
            act_fail += 1
    print(f"\nActivation results: {act_ok} activated, {act_fail} failed")

    # Step 4: Verify
    all_active = verify_all_active()

    print("\n" + "=" * 70)
    if all_active:
        print("RESULT: ALL 11 AGENT WORKFLOWS SYNCED AND ACTIVE")
    else:
        print("RESULT: SOME WORKFLOWS FAILED — see details above")
    print("=" * 70)

    return 0 if all_active else 1


if __name__ == "__main__":
    sys.exit(main())
