"""
Sync, validate, and activate ALL 18 Aspire workflows in n8n (7 ops + 11 agent).
Step 1: Update workflows from local JSON files (PUT /api/v1/workflows/{id})
Step 2: Validate workflow structure (kill switch, receipts, env var hosts, retries)
Step 3: Activate all workflows (POST /api/v1/workflows/{id}/activate)
Step 4: Final verification (all 18 active)
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
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# --- Agent workflows (11) in infrastructure/n8n-workflows/ ---
AGENT_WORKFLOW_MAP = {
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
AGENT_DIR = os.path.join(BASE_DIR, "infrastructure", "n8n-workflows")

# --- Ops workflows (7) in infrastructure/n8n/ ---
OPS_WORKFLOW_MAP = {
    "receipt-archival": "WkRIXVANitOqZrzF",
    "batch-email-digest": "CYOIhxMz6MjyL32W",
    "reconciliation": "1ekOKUNgKYC583c3",
    "cert-renewal": "Aa3NELsKQz9tckwA",
    "analytics-rollup": "UP1XYgy2jFAsn4N1",
    "provider-health": "SuhhWyi5SBwEejRF",
    "slo-monitor": "dRR36XPA6KuQfL0T",
}
OPS_DIR = os.path.join(BASE_DIR, "infrastructure", "n8n")

# Combined: name -> (n8n_id, local_dir)
ALL_WORKFLOWS = {}
for name, wf_id in AGENT_WORKFLOW_MAP.items():
    ALL_WORKFLOWS[name] = (wf_id, AGENT_DIR)
for name, wf_id in OPS_WORKFLOW_MAP.items():
    ALL_WORKFLOWS[name] = (wf_id, OPS_DIR)

TOTAL = len(ALL_WORKFLOWS)  # 18


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
    """Update all 18 workflows from local JSON."""
    print("=" * 70)
    print(f"STEP 1: SYNC - Updating {TOTAL} workflows from local JSON")
    print("=" * 70)

    ok = []
    fail = []

    for name in sorted(ALL_WORKFLOWS.keys()):
        wf_id, wf_dir = ALL_WORKFLOWS[name]
        filepath = os.path.join(wf_dir, f"{name}.json")
        category = "agent" if wf_dir == AGENT_DIR else "ops"

        if not os.path.exists(filepath):
            print(f"  SKIP [{category:5s}] {name}: file not found")
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

        print(f"  UPDATE [{category:5s}] {name} -> {wf_id} ... ", end="", flush=True)
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

    print(f"\nSync: {len(ok)}/{TOTAL} OK, {len(fail)} failed")
    if fail:
        for name, code, msg in fail:
            print(f"  FAILED: {name} - {code}: {msg}")
    return len(fail) == 0


def step2_validate():
    """Check each workflow for structural correctness."""
    print()
    print("=" * 70)
    print(f"STEP 2: VALIDATE - Checking {TOTAL} workflow structures")
    print("=" * 70)

    all_ok = True
    checks = {
        "kill_switch": 0,
        "receipts": 0,
        "error_trigger": 0,
        "env_var_hosts": 0,
        "inline_supabase_headers": 0,
        "retry_config": 0,
        "sentinel_uuid": 0,
        "random_receipt_id": 0,
    }

    for name in sorted(ALL_WORKFLOWS.keys()):
        wf_id, wf_dir = ALL_WORKFLOWS[name]
        category = "agent" if wf_dir == AGENT_DIR else "ops"
        print(f"  VALIDATE [{category:5s}] {name} ... ", end="", flush=True)

        resp, status = api_request("GET", f"/workflows/{wf_id}")
        if status != 200:
            print(f"FAILED to fetch (HTTP {status})")
            all_ok = False
            continue

        nodes = resp.get("nodes", [])
        connections = resp.get("connections", {})
        issues = []
        wf_json = json.dumps(resp)

        if not nodes:
            issues.append("no nodes")
        if not connections:
            issues.append("no connections")

        # --- Kill switch check ---
        node_names = [n.get("name", "") for n in nodes]
        has_kill_switch = any(
            "kill switch" in nm.lower() or "kill_switch" in nm.lower()
            for nm in node_names
        )
        if not has_kill_switch:
            issues.append("NO kill switch")
        else:
            checks["kill_switch"] += 1

        # --- Receipt nodes ---
        has_receipt = any("receipt" in nm.lower() for nm in node_names)
        if not has_receipt:
            issues.append("no receipt nodes")
        else:
            checks["receipts"] += 1

        # --- Error Trigger ---
        has_error_trigger = any(
            n.get("type", "") == "n8n-nodes-base.errorTrigger" for n in nodes
        )
        if not has_error_trigger:
            issues.append("no Error Trigger")
        else:
            checks["error_trigger"] += 1

        # --- All HTTP hosts from $env ---
        http_nodes = [n for n in nodes if "httpRequest" in n.get("type", "")]
        all_env_hosts = True
        for hn in http_nodes:
            url_val = hn.get("parameters", {}).get("url", "")
            if url_val and "$env." not in url_val:
                all_env_hosts = False
                issues.append(f"hardcoded URL in '{hn.get('name', '?')}'")
                break
        if all_env_hosts:
            checks["env_var_hosts"] += 1

        # --- Supabase nodes use inline headers (not named credentials) ---
        supabase_nodes = [
            hn for hn in http_nodes
            if "SUPABASE_URL" in hn.get("parameters", {}).get("url", "")
        ]
        all_inline = True
        for sn in supabase_nodes:
            auth = sn.get("parameters", {}).get("authentication", "")
            if auth != "none":
                all_inline = False
                issues.append(f"Supabase node '{sn.get('name', '?')}' uses named credential (not inline)")
                break
            # Also check the node-level credentials block doesn't exist
            if sn.get("credentials"):
                all_inline = False
                issues.append(f"Supabase node '{sn.get('name', '?')}' has credentials block")
                break
        if all_inline:
            checks["inline_supabase_headers"] += 1

        # --- Retry config on non-receipt HTTP nodes ---
        non_receipt_http = [
            hn for hn in http_nodes
            if "receipt" not in hn.get("name", "").lower()
        ]
        has_retry = True
        for hn in non_receipt_http:
            opts = hn.get("parameters", {}).get("options", {})
            if not opts.get("retry"):
                has_retry = False
                break
        if has_retry and non_receipt_http:
            checks["retry_config"] += 1
        elif not non_receipt_http:
            checks["retry_config"] += 1  # No non-receipt HTTP nodes, trivially passes

        # --- Sentinel UUID (no all-zeros UUID) ---
        if "00000000-0000-0000-0000-000000000000" in wf_json:
            issues.append("contains nil UUID (00000000...) instead of sentinel")
        else:
            checks["sentinel_uuid"] += 1

        # --- Random suffix on receipt IDs ---
        if "Math.random().toString(36).slice(2, 8)" in wf_json:
            checks["random_receipt_id"] += 1
        elif "receipt" in wf_json.lower():
            issues.append("receipt IDs may lack random suffix")

        # --- Node count ---
        if len(nodes) < 4:
            issues.append(f"only {len(nodes)} nodes (suspiciously low)")

        if issues:
            print(f"ISSUES: {', '.join(issues)}")
            all_ok = False
        else:
            print(f"OK ({len(nodes)} nodes, {len(connections)} conn)")

    print(f"\n  Validation summary across {TOTAL} workflows:")
    for check_name, count in checks.items():
        status = "PASS" if count == TOTAL else f"{count}/{TOTAL}"
        print(f"    {check_name:30s} {status}")

    return all_ok


def step3_activate():
    """Activate all 18 workflows."""
    print()
    print("=" * 70)
    print(f"STEP 3: ACTIVATE - Enabling all {TOTAL} workflows")
    print("=" * 70)

    ok = []
    fail = []

    for name in sorted(ALL_WORKFLOWS.keys()):
        wf_id, wf_dir = ALL_WORKFLOWS[name]
        category = "agent" if wf_dir == AGENT_DIR else "ops"
        print(f"  ACTIVATE [{category:5s}] {name} ({wf_id}) ... ", end="", flush=True)

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

    print(f"\nActivation: {len(ok)}/{TOTAL} OK, {len(fail)} failed")
    if fail:
        for name, code, msg in fail:
            print(f"  FAILED: {name} - {code}: {msg}")
    return len(fail) == 0


def step4_verify():
    """Fetch all workflows and show final state."""
    print()
    print("=" * 70)
    print("STEP 4: FINAL VERIFICATION")
    print("=" * 70)

    resp, status = api_request("GET", "/workflows?limit=50")
    if status != 200:
        print(f"ERROR: Could not fetch workflow list (HTTP {status})")
        return False

    all_wf_ids = set()
    for name, (wf_id, _) in ALL_WORKFLOWS.items():
        all_wf_ids.add(wf_id)

    all_active = True
    found_wfs = [w for w in resp.get("data", []) if w["id"] in all_wf_ids]
    found_wfs.sort(key=lambda w: w.get("name", ""))

    for w in found_wfs:
        is_active = w.get("active", False)
        marker = "ACTIVE" if is_active else "INACTIVE"
        if not is_active:
            all_active = False
        print(f"  [{marker:8s}] {w['id']:20s} {w['name']}")

    found_ids = {w["id"] for w in found_wfs}
    missing = all_wf_ids - found_ids
    if missing:
        print(f"\n  MISSING ({len(missing)}) workflows not found in n8n:")
        for mid in missing:
            # Find the name
            for name, (wid, _) in ALL_WORKFLOWS.items():
                if wid == mid:
                    print(f"    {name} ({mid})")
        all_active = False

    print()
    if all_active and len(found_wfs) == TOTAL:
        print(f"SUCCESS: All {TOTAL} workflows are ACTIVE")
    else:
        active_count = sum(1 for w in found_wfs if w.get("active", False))
        print(f"STATUS: {active_count}/{TOTAL} active, {len(found_wfs)} found")

    return all_active and len(found_wfs) == TOTAL


def main():
    if not API_KEY:
        print("ERROR: N8N_API_KEY not set (checked .mcp.json and env)")
        sys.exit(1)

    # Test connectivity
    resp, status = api_request("GET", "/workflows?limit=1")
    if status != 200:
        print(f"ERROR: Cannot connect to n8n at {URL}: {resp}")
        sys.exit(1)
    print(f"Connected to n8n at {URL}")
    print(f"Managing {TOTAL} workflows (11 agent + 7 ops)\n")

    sync_ok = step1_sync()
    validate_ok = step2_validate()
    activate_ok = step3_activate()
    verify_ok = step4_verify()

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Sync:       {'PASS' if sync_ok else 'FAIL'}")
    print(f"  Validate:   {'PASS' if validate_ok else 'WARNINGS'}")
    print(f"  Activate:   {'PASS' if activate_ok else 'FAIL'}")
    print(f"  Verify:     {'PASS' if verify_ok else 'FAIL'}")

    if sync_ok and activate_ok and verify_ok:
        print(f"\nAll {TOTAL} workflows synced, validated, and activated successfully.")
        sys.exit(0)
    else:
        print("\nSome steps had failures. Review output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
