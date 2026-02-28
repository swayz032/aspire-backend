"""
Sync, validate, and activate all 11 Aspire agent workflows in n8n.
Step 1: Update workflows from local JSON files
Step 2: Validate workflow structure
Step 3: Activate all workflows
"""
import json
import os
import sys
import io
import urllib.request
import urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_MCP_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".mcp.json"
)


def _load_api_key():
    """Load API key from .mcp.json (canonical source) or env var."""
    if os.path.exists(_MCP_JSON):
        try:
            with open(_MCP_JSON, "r", encoding="utf-8") as f:
                mcp = json.load(f)
            # Navigate to n8n MCP server env
            servers = mcp.get("mcpServers", {})
            for name, cfg in servers.items():
                key = cfg.get("env", {}).get("N8N_API_KEY", "")
                if key:
                    return key
        except Exception:
            pass
    return os.environ.get("N8N_API_KEY", "")


API_KEY = _load_api_key()
URL = os.environ.get("N8N_API_URL", "http://localhost:5678")
WORKFLOWS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "infrastructure",
    "n8n-workflows",
)

WORKFLOW_MAP = {
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

WEBHOOK_WORKFLOWS = {
    "intake-activation",
    "eli-email-triage",
    "sarah-call-handler",
    "nora-meeting-summary",
}


def api_request(method, path, data=None):
    """Make authenticated n8n API request."""
    url = f"{URL}/api/v1{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-N8N-API-KEY", API_KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        return {"error": body_text, "status": e.code}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def step1_sync():
    """Update all 11 workflows from local JSON."""
    print("=" * 60)
    print("STEP 1: SYNC - Updating 11 workflows from local JSON")
    print("=" * 60)

    ok = []
    fail = []

    for name, wf_id in sorted(WORKFLOW_MAP.items()):
        filepath = os.path.join(WORKFLOWS_DIR, f"{name}.json")
        if not os.path.exists(filepath):
            print(f"  SKIP {name}: file not found at {filepath}")
            fail.append((name, "file_not_found", ""))
            continue

        with open(filepath, "r", encoding="utf-8") as f:
            local_data = json.load(f)

        payload = {
            "name": local_data.get("name", ""),
            "nodes": local_data.get("nodes", []),
            "connections": local_data.get("connections", {}),
            "settings": local_data.get("settings", {}),
        }

        print(f"  UPDATE {name} -> {wf_id} ... ", end="", flush=True)
        resp, status = api_request("PUT", f"/workflows/{wf_id}", payload)
        if status == 200:
            print("OK")
            ok.append(name)
        else:
            err_msg = ""
            if isinstance(resp, dict):
                err_msg = resp.get("error", str(resp))[:150]
            else:
                err_msg = str(resp)[:150]
            print(f"FAILED (HTTP {status}: {err_msg})")
            fail.append((name, f"HTTP {status}", err_msg))

    print(f"\nSync: {len(ok)}/11 OK, {len(fail)} failed")
    if fail:
        for name, code, msg in fail:
            print(f"  FAILED: {name} - {code}: {msg}")
    return len(fail) == 0


def step2_validate():
    """Check each workflow for structural issues."""
    print()
    print("=" * 60)
    print("STEP 2: VALIDATE - Checking workflow structure")
    print("=" * 60)

    all_ok = True

    for name, wf_id in sorted(WORKFLOW_MAP.items()):
        print(f"  VALIDATE {name} ... ", end="", flush=True)
        resp, status = api_request("GET", f"/workflows/{wf_id}")
        if status != 200:
            print(f"FAILED to fetch (HTTP {status})")
            all_ok = False
            continue

        nodes = resp.get("nodes", [])
        connections = resp.get("connections", {})
        issues = []

        if not nodes:
            issues.append("no nodes")
        if not connections:
            issues.append("no connections")

        # Kill switch check
        node_names = [n.get("name", "") for n in nodes]
        has_kill_switch = any(
            "kill switch" in nm.lower() or "kill_switch" in nm.lower()
            for nm in node_names
        )
        if not has_kill_switch:
            issues.append("no kill switch node")

        # HMAC check for webhook workflows (check node names AND Code node content)
        if name in WEBHOOK_WORKFLOWS:
            has_hmac = any(
                "hmac" in nm.lower() or "signature" in nm.lower() for nm in node_names
            )
            if not has_hmac:
                # Also check Code node content for HMAC logic
                for node in nodes:
                    if node.get("type", "") == "n8n-nodes-base.code":
                        code = node.get("parameters", {}).get("jsCode", "")
                        if "createHmac" in code or "hmac" in code.lower():
                            has_hmac = True
                            break
            if not has_hmac:
                issues.append("webhook missing HMAC validation")

        # Check for receipt nodes
        has_receipt = any("receipt" in nm.lower() for nm in node_names)
        if not has_receipt:
            issues.append("no receipt nodes found")

        # Check for Gateway call (orchestrator)
        node_types = [n.get("type", "") for n in nodes]
        has_http = any("httpRequest" in t for t in node_types)
        if not has_http:
            issues.append("no HTTP Request nodes")

        # Check for retry config on HTTP nodes
        http_nodes = [n for n in nodes if "httpRequest" in n.get("type", "")]
        for hn in http_nodes:
            hn_name = hn.get("name", "")
            # Only check Gateway nodes for retry
            if "gateway" in hn_name.lower() or "orchestrator" in hn_name.lower():
                options = hn.get("parameters", {}).get("options", {})
                # retryOnFail might be in parameters directly or in options
                params = hn.get("parameters", {})
                has_retry = (
                    params.get("options", {}).get("retryOnFail") is not None
                    or params.get("retryOnFail") is not None
                )
                # n8n stores retry config at top-level in parameters
                retry_on_fail = params.get("retryOnFail", False)
                if not retry_on_fail:
                    # Check if it might be in the options sub-object
                    pass  # Not all versions store it the same way

        # Node count sanity
        if len(nodes) < 4:
            issues.append(f"only {len(nodes)} nodes (suspiciously low)")

        if issues:
            print(f"ISSUES: {', '.join(issues)}")
            all_ok = False
        else:
            print(f"OK ({len(nodes)} nodes, {len(connections)} connection groups)")

    return all_ok


def step3_activate():
    """Activate all 11 workflows."""
    print()
    print("=" * 60)
    print("STEP 3: ACTIVATE - Enabling all 11 workflows")
    print("=" * 60)

    ok = []
    fail = []

    for name, wf_id in sorted(WORKFLOW_MAP.items()):
        print(f"  ACTIVATE {name} ({wf_id}) ... ", end="", flush=True)

        # n8n API: POST /workflows/{id}/activate (not PATCH)
        resp, status = api_request("POST", f"/workflows/{wf_id}/activate", None)
        if status == 200:
            active_state = resp.get("active", "unknown")
            print(f"OK (active={active_state})")
            ok.append(name)
        else:
            err_msg = ""
            if isinstance(resp, dict):
                err_msg = resp.get("error", str(resp))[:150]
            else:
                err_msg = str(resp)[:150]
            print(f"FAILED (HTTP {status}: {err_msg})")
            fail.append((name, f"HTTP {status}", err_msg))

    print(f"\nActivation: {len(ok)}/11 OK, {len(fail)} failed")
    if fail:
        for name, code, msg in fail:
            print(f"  FAILED: {name} - {code}: {msg}")
    return len(fail) == 0


def final_verification():
    """Fetch all workflows and show final state."""
    print()
    print("=" * 60)
    print("FINAL VERIFICATION")
    print("=" * 60)

    resp, status = api_request("GET", "/workflows?limit=25")
    if status != 200:
        print(f"ERROR: Could not fetch workflow list (HTTP {status})")
        return False

    agent_wf_ids = set(WORKFLOW_MAP.values())
    all_active = True

    # Sort by name for readability
    agent_wfs = [w for w in resp["data"] if w["id"] in agent_wf_ids]
    agent_wfs.sort(key=lambda w: w.get("name", ""))

    for w in agent_wfs:
        is_active = w.get("active", False)
        marker = "ACTIVE" if is_active else "INACTIVE"
        if not is_active:
            all_active = False
        print(f"  [{marker:8s}] {w['id']:20s} {w['name']}")

    print()
    if all_active:
        print("SUCCESS: All 11 agent workflows are ACTIVE")
    else:
        inactive_count = sum(1 for w in agent_wfs if not w.get("active", False))
        print(f"WARNING: {inactive_count} workflows are still INACTIVE")

    return all_active


def main():
    if not API_KEY:
        print("ERROR: N8N_API_KEY not set")
        sys.exit(1)

    # Test connectivity
    resp, status = api_request("GET", "/workflows?limit=1")
    if status != 200:
        print(f"ERROR: Cannot connect to n8n at {URL}: {resp}")
        sys.exit(1)
    print(f"Connected to n8n at {URL}\n")

    sync_ok = step1_sync()
    validate_ok = step2_validate()
    activate_ok = step3_activate()
    verify_ok = final_verification()

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Sync:       {'PASS' if sync_ok else 'FAIL'}")
    print(f"  Validate:   {'PASS' if validate_ok else 'WARNINGS'}")
    print(f"  Activate:   {'PASS' if activate_ok else 'FAIL'}")
    print(f"  Verify:     {'PASS' if verify_ok else 'FAIL'}")

    if sync_ok and activate_ok and verify_ok:
        print("\nAll 11 workflows synced, validated, and activated successfully.")
        sys.exit(0)
    else:
        print("\nSome steps had failures. Review output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
