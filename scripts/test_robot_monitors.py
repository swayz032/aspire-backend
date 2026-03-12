#!/usr/bin/env python3
"""End-to-end smoke for robot monitor workflows.

Flow:
1. Sync live n8n workflows from repo source.
2. Inject temporary webhook triggers into the three monitor workflows.
3. Trigger each workflow on demand.
4. Verify new robot runs appear via the local admin robot endpoint.
5. Verify failed robot runs also surface in the incident feed.
6. Restore the original workflow definitions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = ROOT.parent / ".mcp.json"
ORCH_ENV_PATH = ROOT / "infrastructure" / "docker" / "orchestrator-safety.env"
N8N_API_URL = os.environ.get("N8N_API_URL", "http://localhost:5678").rstrip("/")
ORCH_BASE_URL = os.environ.get("ASPIRE_ORCHESTRATOR_URL", "http://localhost:8000").rstrip("/")

WORKFLOWS = {
    "slo-monitor": {"id": "dRR36XPA6KuQfL0T", "webhook_path": "robot-test-slo-monitor"},
    "provider-health": {"id": "SuhhWyi5SBwEejRF", "webhook_path": "robot-test-provider-health"},
    "rotation-monitor": {"id": "uI4JbtvTA4Vo8Rg4", "webhook_path": "robot-test-rotation-monitor"},
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


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _build_admin_token(secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": "robot-monitor-smoke",
        "role": "admin",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


API_KEY = _load_api_key()
ORCH_ENV = _load_env(ORCH_ENV_PATH)
ADMIN_TOKEN = _build_admin_token(ORCH_ENV["ASPIRE_ADMIN_JWT_SECRET"])


def _request(method: str, url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _api_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    status, body = _request(
        method,
        f"{N8N_API_URL}/api/v1{path}",
        headers={
            "X-N8N-API-KEY": API_KEY,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
    )
    try:
        return status, json.loads(body) if body else {}
    except json.JSONDecodeError:
        return status, {"raw": body}


def _get_executions(workflow_id: str) -> list[dict]:
    status, body = _api_request("GET", f"/executions?workflowId={workflow_id}&limit=20")
    if status != 200:
        raise RuntimeError(f"failed to list executions for {workflow_id}: {status}")
    return body.get("data", [])


def _wait_for_new_execution(workflow_id: str, baseline: set[str]) -> str:
    deadline = time.time() + 20
    while time.time() < deadline:
        current = _get_executions(workflow_id)
        delta = [item for item in current if str(item["id"]) not in baseline]
        if delta:
            delta.sort(key=lambda item: int(item["id"]), reverse=True)
            for preferred_mode in ("trigger", "webhook"):
                for item in delta:
                    if item.get("mode") == preferred_mode:
                        return str(item["id"])
            return str(delta[0]["id"])
        time.sleep(1.0)
    raise RuntimeError(f"workflow {workflow_id} did not produce a new execution")


def _get_execution_detail(execution_id: str) -> dict:
    status, body = _api_request("GET", f"/executions/{execution_id}?includeData=true")
    if status != 200:
        raise RuntimeError(f"failed to fetch execution {execution_id}: {status}")
    return body


def _extract_request_id(execution_detail: dict) -> str:
    run_data = execution_detail.get("data", {}).get("resultData", {}).get("runData", {})
    prep_runs = run_data.get("Kill Switch + Prep", [])
    if not prep_runs:
        raise RuntimeError("missing Kill Switch + Prep run data")
    item = prep_runs[0].get("data", {}).get("main", [[{}]])[0][0]
    request_id = item.get("json", {}).get("requestId")
    if not request_id:
        raise RuntimeError("missing requestId in Kill Switch + Prep output")
    return str(request_id)


def _fetch_robot_runs() -> list[dict]:
    status, body = _request(
        "GET",
        f"{ORCH_BASE_URL}/admin/ops/robots?limit=50",
        headers={
            "x-admin-token": ADMIN_TOKEN,
            "x-correlation-id": f"robot-monitor-admin-{int(time.time())}",
        },
    )
    if status != 200:
        raise RuntimeError(f"admin robot endpoint failed: {status} {body[:300]}")
    return json.loads(body).get("items", [])


def _fetch_incidents() -> list[dict]:
    status, body = _request(
        "GET",
        f"{ORCH_BASE_URL}/admin/ops/incidents?limit=50",
        headers={
            "x-admin-token": ADMIN_TOKEN,
            "x-correlation-id": f"robot-monitor-incidents-{int(time.time())}",
        },
    )
    if status != 200:
        raise RuntimeError(f"admin incidents endpoint failed: {status} {body[:300]}")
    return json.loads(body).get("items", [])


def _inject_test_webhook(workflow_id: str, path_slug: str) -> dict:
    status, body = _api_request("GET", f"/workflows/{workflow_id}")
    if status != 200:
        raise RuntimeError(f"failed to fetch workflow {workflow_id}: {status}")
    original = body

    status, _ = _api_request("POST", f"/workflows/{workflow_id}/deactivate")
    if status not in {200, 201}:
        raise RuntimeError(f"failed to deactivate {workflow_id}: {status}")

    nodes = [node for node in body.get("nodes", []) if node.get("name") != "Robot Monitor Test Trigger"]
    connections = {k: v for k, v in body.get("connections", {}).items() if k != "Robot Monitor Test Trigger"}
    nodes.append(
        {
            "id": f"robot-monitor-test-{path_slug}",
            "name": "Robot Monitor Test Trigger",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [40, 520],
            "webhookId": path_slug,
            "parameters": {
                "path": path_slug,
                "httpMethod": "POST",
                "responseMode": "onReceived",
                "options": {},
            },
        }
    )
    connections["Robot Monitor Test Trigger"] = {
        "main": [[{"node": "Kill Switch + Prep", "type": "main", "index": 0}]]
    }

    payload = {
        "name": body["name"],
        "nodes": nodes,
        "connections": connections,
        "settings": body.get("settings", {}),
    }
    status, result = _api_request("PUT", f"/workflows/{workflow_id}", payload)
    if status != 200:
        raise RuntimeError(f"failed to update {workflow_id}: {status} {str(result)[:240]}")

    status, _ = _api_request("POST", f"/workflows/{workflow_id}/activate")
    if status not in {200, 201}:
        raise RuntimeError(f"failed to reactivate {workflow_id}: {status}")
    time.sleep(3.0)
    return original


def _restore_workflow(workflow_id: str, original: dict) -> None:
    payload = {
        "name": original["name"],
        "nodes": original.get("nodes", []),
        "connections": original.get("connections", {}),
        "settings": original.get("settings", {}),
    }
    _api_request("POST", f"/workflows/{workflow_id}/deactivate")
    status, result = _api_request("PUT", f"/workflows/{workflow_id}", payload)
    if status != 200:
        raise RuntimeError(f"failed to restore {workflow_id}: {status} {str(result)[:240]}")
    status, _ = _api_request("POST", f"/workflows/{workflow_id}/activate")
    if status not in {200, 201}:
        raise RuntimeError(f"failed to reactivate restored {workflow_id}: {status}")


def _trigger_webhook(path_slug: str) -> None:
    status, body = _request(
        "POST",
        f"{N8N_API_URL}/webhook/{path_slug}",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"probe": True, "ts": time.time()}).encode("utf-8"),
    )
    if status >= 400:
        raise RuntimeError(f"webhook trigger failed for {path_slug}: {status} {body[:240]}")


def _sync_live_sources() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_robot_monitors.py")],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        raise RuntimeError("sync_robot_monitors.py failed")


def main() -> int:
    if not API_KEY:
        print("ERROR: missing N8N API key")
        return 2

    originals: dict[str, dict] = {}
    request_ids: dict[str, str] = {}
    try:
        _sync_live_sources()

        baselines = {name: {str(item["id"]) for item in _get_executions(cfg["id"])} for name, cfg in WORKFLOWS.items()}
        for name, cfg in WORKFLOWS.items():
            originals[name] = _inject_test_webhook(cfg["id"], cfg["webhook_path"])

        for name, cfg in WORKFLOWS.items():
            _trigger_webhook(cfg["webhook_path"])
            execution_id = _wait_for_new_execution(cfg["id"], baselines[name])
            detail = _get_execution_detail(execution_id)
            request_ids[name] = _extract_request_id(detail)
            print(f"OK execution {name} -> {execution_id} run_id={request_ids[name]}")

        deadline = time.time() + 30
        robot_runs = []
        while time.time() < deadline:
            robot_runs = _fetch_robot_runs()
            if all(any(item.get("run_id") == run_id for item in robot_runs) for run_id in request_ids.values()):
                break
            time.sleep(1.0)

        missing_runs = [name for name, run_id in request_ids.items() if not any(item.get("run_id") == run_id for item in robot_runs)]
        if missing_runs:
            raise RuntimeError(f"missing robot runs in admin endpoint: {missing_runs}")

        failed_run_ids = [item.get("run_id") for item in robot_runs if item.get("run_id") in request_ids.values() and item.get("status") == "failed"]
        incidents = []
        incident_deadline = time.time() + 30
        while time.time() < incident_deadline:
            incidents = _fetch_incidents()
            if all(any(str(incident.get("correlation_id", "")) == run_id for incident in incidents) for run_id in failed_run_ids):
                break
            time.sleep(1.0)

        missing_incidents = [
            run_id for run_id in failed_run_ids
            if not any(str(incident.get("correlation_id", "")) == run_id for incident in incidents)
        ]
        if missing_incidents:
            raise RuntimeError(f"missing incidents for failed robot runs: {missing_incidents}")

        print("OK robot monitor smoke")
        print(f"  robot runs visible: {len(request_ids)}")
        print(f"  failed runs surfaced as incidents: {len(failed_run_ids)}")
        return 0
    finally:
        for name, cfg in WORKFLOWS.items():
            original = originals.get(name)
            if original is None:
                continue
            try:
                _restore_workflow(cfg["id"], original)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN failed to restore {name}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
