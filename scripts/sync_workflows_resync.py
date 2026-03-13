#!/usr/bin/env python3
"""
Sync all 11 agent workflow JSON files to n8n via PUT /api/v1/workflows/{id},
then verify all are active. Re-activate any that became inactive.
"""
import json
import os
import sys
import io
import urllib.request
import urllib.error

from _n8n_runtime import get_n8n_api_key, get_n8n_base_url

# Force UTF-8 output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

N8N_BASE = get_n8n_base_url()
API_KEY = get_n8n_api_key()

WORKFLOW_DIR = "/mnt/c/Users/tonio/Projects/myapp/infrastructure/n8n-workflows"

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


def api_request(method, path, data=None):
    """Make an API request to n8n."""
    url = f"{N8N_BASE}{path}"
    headers = {
        "X-N8N-API-KEY": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        print(f"  HTTP {e.code}: {error_body[:300]}")
        raise
    except urllib.error.URLError as e:
        print(f"  Connection error: {e.reason}")
        raise


def sync_workflow(name, wf_id):
    """Read local JSON and PUT to n8n."""
    filepath = os.path.join(WORKFLOW_DIR, f"{name}.json")
    if not os.path.exists(filepath):
        print(f"  SKIP - file not found: {filepath}")
        return False

    with open(filepath, "r", encoding="utf-8") as f:
        wf_json = json.load(f)

    # Extract only the fields n8n expects for PUT
    payload = {
        "name": wf_json["name"],
        "nodes": wf_json["nodes"],
        "connections": wf_json["connections"],
        "settings": wf_json.get("settings", {}),
    }

    try:
        result = api_request("PUT", f"/api/v1/workflows/{wf_id}", payload)
        active_status = result.get("active", False)
        print(f"  SYNCED - active={active_status}")
        return True
    except Exception as e:
        print(f"  FAILED - {e}")
        return False


def activate_workflow(wf_id):
    """POST to activate a workflow."""
    try:
        result = api_request("POST", f"/api/v1/workflows/{wf_id}/activate")
        print(f"  ACTIVATED - active={result.get('active', '?')}")
        return True
    except Exception:
        return False


def verify_all_active():
    """GET all workflows and check active status."""
    result = api_request("GET", "/api/v1/workflows?limit=25")
    workflows = result.get("data", [])

    active_count = 0
    inactive = []
    for wf in workflows:
        wf_id = wf.get("id", "")
        wf_name = wf.get("name", "")
        is_active = wf.get("active", False)
        # Check if this is one of our 11
        if wf_id in WORKFLOW_MAP.values():
            if is_active:
                active_count += 1
            else:
                inactive.append((wf_id, wf_name))

    return active_count, inactive


def main():
    print("=" * 60)
    print("n8n Workflow Re-Sync (11 agent workflows)")
    print("=" * 60)

    # Step 1: Sync all 11
    sync_ok = 0
    sync_fail = 0
    for name, wf_id in sorted(WORKFLOW_MAP.items()):
        print(f"\n[SYNC] {name} -> {wf_id}")
        if sync_workflow(name, wf_id):
            sync_ok += 1
        else:
            sync_fail += 1

    print(f"\n{'=' * 60}")
    print(f"Sync complete: {sync_ok} OK, {sync_fail} FAILED")
    print(f"{'=' * 60}")

    # Step 2: Verify active status
    print("\n[VERIFY] Checking active status...")
    active_count, inactive = verify_all_active()
    print(f"  Active: {active_count}/11")

    # Step 3: Re-activate any that became inactive
    if inactive:
        print(f"\n[RE-ACTIVATE] {len(inactive)} workflows need re-activation:")
        for wf_id, wf_name in inactive:
            print(f"  {wf_name} ({wf_id})")
            activate_workflow(wf_id)

        # Verify again
        print("\n[VERIFY] Re-checking after activation...")
        active_count, inactive = verify_all_active()
        print(f"  Active: {active_count}/11")
        if inactive:
            print(f"  Still inactive: {[n for _, n in inactive]}")

    # Final report
    print(f"\n{'=' * 60}")
    print(f"FINAL REPORT: {sync_ok} synced, {active_count}/11 active")
    if sync_fail == 0 and active_count == 11:
        print("STATUS: ALL GOOD")
    else:
        print("STATUS: ISSUES DETECTED - review above")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
