#!/usr/bin/env python3
"""Probe live provider rotation prerequisites without exposing secret values."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import boto3
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER_ENV = REPO_ROOT / "infrastructure" / "docker" / ".env"


def _parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _secrets_client() -> Any:
    env = _parse_env(DOCKER_ENV)
    return boto3.client(
        "secretsmanager",
        region_name="us-east-1",
        aws_access_key_id=env.get("AWS_ROTATION_TRIGGER_ACCESS_KEY_ID"),
        aws_secret_access_key=env.get("AWS_ROTATION_TRIGGER_SECRET_ACCESS_KEY"),
    )


def _load_provider_secret() -> dict[str, Any]:
    sm = _secrets_client()
    payload = sm.get_secret_value(SecretId="aspire/dev/providers")
    return json.loads(payload["SecretString"])


def probe_deepgram(secret: dict[str, Any]) -> dict[str, Any]:
    api_key = str(secret.get("deepgram_key") or "").strip()
    if not api_key:
        return {"provider": "deepgram", "status": "missing_secret"}

    headers = {"Authorization": f"Token {api_key}", "Accept": "application/json"}
    auth_resp = requests.get("https://api.deepgram.com/v1/auth/token", headers=headers, timeout=20)
    auth_data = auth_resp.json() if auth_resp.ok else {}
    scopes = list(auth_data.get("scopes") or [])
    accessor = str(auth_data.get("accessor") or "")

    project_resp = requests.get("https://api.deepgram.com/v1/projects", headers=headers, timeout=20)
    projects = project_resp.json().get("projects") if project_resp.ok and isinstance(project_resp.json(), dict) else []
    project_id = ""
    keys_status = None
    if isinstance(projects, list) and len(projects) == 1:
        project_id = str(projects[0].get("project_id") or "")
        keys_status = requests.get(
            f"https://api.deepgram.com/v1/projects/{project_id}/keys",
            headers=headers,
            timeout=20,
        ).status_code

    if project_id and keys_status and keys_status < 300:
        status = "ready"
        blocker = ""
    elif not project_id:
        status = "blocked"
        blocker = "project_id unavailable"
    else:
        status = "blocked"
        blocker = f"project key API returned HTTP {keys_status}"

    return {
        "provider": "deepgram",
        "status": status,
        "blocker": blocker,
        "scopes": scopes,
        "scopes_informational_only": True,
        "accessor_present": bool(accessor),
        "project_count": len(projects) if isinstance(projects, list) else 0,
        "project_id_present": bool(project_id),
        "keys_endpoint_status": keys_status,
    }


def probe_elevenlabs(secret: dict[str, Any]) -> dict[str, Any]:
    api_key = str(secret.get("elevenlabs_key") or "").strip()
    if not api_key:
        return {"provider": "elevenlabs", "status": "missing_secret"}

    headers = {"xi-api-key": api_key, "Accept": "application/json"}
    response = requests.get("https://api.elevenlabs.io/v1/service-accounts", headers=headers, timeout=20)
    if not response.ok:
        return {
            "provider": "elevenlabs",
            "status": "blocked",
            "blocker": f"service accounts API returned HTTP {response.status_code}",
            "service_account_count": 0,
        }

    payload = response.json()
    service_accounts = payload.get("service-accounts") if isinstance(payload, dict) else []
    count = len(service_accounts) if isinstance(service_accounts, list) else 0

    return {
        "provider": "elevenlabs",
        "status": "ready" if count > 0 else "blocked",
        "blocker": "" if count > 0 else "workspace has no service accounts",
        "service_account_count": count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    secret = _load_provider_secret()
    report = {
        "deepgram": probe_deepgram(secret),
        "elevenlabs": probe_elevenlabs(secret),
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    for provider, data in report.items():
        print(f"{provider}: {data['status']}")
        if data.get("blocker"):
            print(f"  blocker: {data['blocker']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
