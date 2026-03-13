#!/usr/bin/env python3
"""Sync the rotation control workflows into live n8n.

Scope:
- rotation-orchestrator
- rotation-monitor
- provider-health
- slo-monitor
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = ROOT.parent / ".mcp.json"
N8N_API_URL = os.environ.get("N8N_API_URL", "http://localhost:5678").rstrip("/")

WORKFLOWS = {
    "rotation-orchestrator": {
        "id": "Jyewljst0Znk1mBS",
        "path": ROOT / "infrastructure" / "n8n-workflows" / "rotation-orchestrator.json",
    },
    "rotation-monitor": {
        "id": "uI4JbtvTA4Vo8Rg4",
        "path": ROOT / "infrastructure" / "n8n-workflows" / "rotation-monitor.json",
    },
    "provider-health": {
        "id": "SuhhWyi5SBwEejRF",
        "path": ROOT / "infrastructure" / "n8n" / "provider-health.json",
    },
    "slo-monitor": {
        "id": "dRR36XPA6KuQfL0T",
        "path": ROOT / "infrastructure" / "n8n" / "slo-monitor.json",
    },
}


def _load_api_key() -> str:
    env_key = os.environ.get("N8N_API_KEY", "")
    if env_key:
        return env_key
    if MCP_PATH.exists():
        try:
            data = json.loads(MCP_PATH.read_text(encoding="utf-8"))
            for cfg in data.get("mcpServers", {}).values():
                key = cfg.get("env", {}).get("N8N_API_KEY", "")
                if key:
                    return key
        except Exception:
            pass
    return ""


API_KEY = _load_api_key()


def _api_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{N8N_API_URL}/api/v1{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
    )
    req.add_header("X-N8N-API-KEY", API_KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"error": body}


def main() -> int:
    if not API_KEY:
        print("ERROR: missing N8N_API_KEY")
        return 2

    failures: list[str] = []
    for name, cfg in WORKFLOWS.items():
        source_path = cfg["path"]
        workflow_id = cfg["id"]
        if not source_path.exists():
            failures.append(f"{name}: missing source file {source_path}")
            continue

        source = json.loads(source_path.read_text(encoding="utf-8-sig"))
        payload = {
            "name": source.get("name", name),
            "nodes": source.get("nodes", []),
            "connections": source.get("connections", {}),
            "settings": source.get("settings", {}),
        }

        status, body = _api_request("PUT", f"/workflows/{workflow_id}", payload)
        if status != 200:
            failures.append(f"{name}: PUT failed ({status}) {str(body)[:240]}")
            continue

        status, _ = _api_request("POST", f"/workflows/{workflow_id}/activate")
        if status not in {200, 201}:
            failures.append(f"{name}: activate failed ({status})")
            continue

        print(f"OK {name} -> {workflow_id}")

    if failures:
        print("")
        for failure in failures:
            print(f"FAIL {failure}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
