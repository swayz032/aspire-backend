"""
Fail CI if live n8n workflows drift from repo JSON (repo is source of truth).

Usage:
  python scripts/check_n8n_drift.py

Requires:
  N8N_API_URL
  N8N_API_KEY
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
N8N_API_URL = os.environ.get("N8N_API_URL", "").rstrip("/")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")

AGENT_WORKFLOW_MAP: dict[str, str] = {
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

OPS_WORKFLOW_MAP: dict[str, str] = {
    "receipt-archival": "WkRIXVANitOqZrzF",
    "batch-email-digest": "CYOIhxMz6MjyL32W",
    "reconciliation": "1ekOKUNgKYC583c3",
    "cert-renewal": "Aa3NELsKQz9tckwA",
    "analytics-rollup": "UP1XYgy2jFAsn4N1",
    "provider-health": "SuhhWyi5SBwEejRF",
    "slo-monitor": "dRR36XPA6KuQfL0T",
}


def _api_request(path: str) -> dict[str, Any]:
    url = f"{N8N_API_URL}/api/v1{path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-N8N-API-KEY", N8N_API_KEY)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    # Keep only deterministic config fields for drift comparison.
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "type": node.get("type"),
        "typeVersion": node.get("typeVersion"),
        "position": node.get("position"),
        "parameters": node.get("parameters", {}),
        "credentials": node.get("credentials", {}),
    }


def _normalize_workflow(data: dict[str, Any]) -> dict[str, Any]:
    nodes = data.get("nodes", [])
    norm_nodes = [_normalize_node(n) for n in nodes]
    norm_nodes.sort(key=lambda n: (str(n.get("id")), str(n.get("name"))))
    settings = data.get("settings", {}) or {}
    # n8n may inject default settings keys server-side.
    # Normalize to canonical comparison keys so drift checks only fail on
    # meaningful config differences.
    norm_settings = {
        "executionOrder": settings.get("executionOrder", "v1"),
        "callerPolicy": settings.get("callerPolicy", "workflowsFromSameOwner"),
        "availableInMCP": bool(settings.get("availableInMCP", False)),
    }
    return {
        "name": data.get("name", ""),
        "nodes": norm_nodes,
        "connections": data.get("connections", {}),
        "settings": norm_settings,
    }


def _load_local(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _json_digest(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _check_group(group_name: str, mapping: dict[str, str], folder: str) -> list[str]:
    drifts: list[str] = []
    for name, wf_id in sorted(mapping.items()):
        local_path = os.path.join(BASE_DIR, folder, f"{name}.json")
        if not os.path.exists(local_path):
            drifts.append(f"[{group_name}] {name}: missing local file")
            continue

        local_data = _load_local(local_path)
        local_norm = _normalize_workflow(local_data)

        try:
            live_data = _api_request(f"/workflows/{wf_id}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            drifts.append(f"[{group_name}] {name}: live fetch HTTP {e.code} ({body[:160]})")
            continue
        except Exception as e:  # noqa: BLE001
            drifts.append(f"[{group_name}] {name}: live fetch failed ({e})")
            continue

        live_norm = _normalize_workflow(live_data)
        if _json_digest(local_norm) != _json_digest(live_norm):
            drifts.append(f"[{group_name}] {name}: drift detected (id={wf_id})")
    return drifts


def main() -> int:
    if not N8N_API_URL:
        print("ERROR: N8N_API_URL is required")
        return 2
    if not N8N_API_KEY:
        print("ERROR: N8N_API_KEY is required")
        return 2

    print(f"Checking n8n drift against {N8N_API_URL}")
    drifts: list[str] = []
    drifts.extend(_check_group("agent", AGENT_WORKFLOW_MAP, os.path.join("infrastructure", "n8n-workflows")))
    drifts.extend(_check_group("ops", OPS_WORKFLOW_MAP, os.path.join("infrastructure", "n8n")))

    if drifts:
        print("")
        print("DRIFT DETECTED:")
        for item in drifts:
            print(f"  - {item}")
        return 1

    print("No drift detected. Live n8n matches repo source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
