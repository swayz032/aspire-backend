#!/usr/bin/env python3
"""Robot Regression Runner — validates Aspire project integrity.

Scenarios:
  - sync_validate: Check sentinel files exist in key project directories
  - api_smoke: Hit /healthz endpoints and verify response time + status

Usage:
  python robot_runner.py \
    --config backend/orchestrator/src/aspire_orchestrator/config/robots.config.yaml \
    --env staging \
    --version-ref abc123

Output: Validated RobotRun JSON written to robots/out/results/<run_id>.json
"""
from __future__ import annotations

import argparse
import hashlib
import hmac as hmac_mod
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml
from jsonschema import validate as js_validate


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


@dataclass
class ScenarioResult:
    name: str
    status: str
    summary: str
    evidence: list[str]


def scenario_sync_validate(cfg: dict[str, Any], project_root: Path) -> ScenarioResult:
    """Check that all sentinel files exist in their expected directories."""
    paths = cfg["paths"]
    sent = cfg["sentinels"]

    failures: list[str] = []
    checked: list[str] = []

    for section_key in sent:
        root_path_key = f"{section_key}_root"
        if root_path_key not in paths:
            failures.append(f"missing path config for {root_path_key}")
            continue

        root = (project_root / paths[root_path_key]).resolve()
        if not root.exists():
            failures.append(f"{section_key} root missing: {root}")
            continue

        for rel in sent[section_key]:
            fp = root / rel
            checked.append(str(fp))
            if not fp.exists():
                failures.append(f"missing {section_key} sentinel: {rel}")

    if failures:
        return ScenarioResult(
            name="sync_validate",
            status="failed",
            summary="; ".join(failures[:10]),
            evidence=[],
        )

    return ScenarioResult(
        name="sync_validate",
        status="passed",
        summary=f"sentinels OK ({len(checked)} files checked)",
        evidence=[],
    )


def scenario_api_smoke(
    cfg: dict[str, Any], env: str, evidence_dir: Path
) -> ScenarioResult:
    """Hit health endpoints and verify latency + status code."""
    env_defaults = cfg["env_defaults"][env]
    base_url = env_defaults["base_url"].rstrip("/")
    s_cfg = cfg["scenarios"]["api_smoke"]
    health_path = s_cfg.get("health_path", "/healthz")
    url = f"{base_url}{health_path}"

    start = time.time()
    try:
        r = requests.get(url, timeout=10)
        latency_ms = int((time.time() - start) * 1000)
        status = r.status_code
        body = r.text[:2000]
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        status = 0
        body = f"ERROR: {e}"

    log_path = evidence_dir / "api_smoke.log"
    log_path.write_text(
        f"URL: {url}\nstatus: {status}\nlatency_ms: {latency_ms}\nbody:\n{body}\n",
        encoding="utf-8",
    )

    p95_ms = int(s_cfg.get("p95_ms", 800))
    max_5xx = int(s_cfg.get("max_5xx", 0))

    failed = False
    reasons: list[str] = []
    if status == 0:
        failed = True
        reasons.append("request_failed")
    if status >= 500 and max_5xx == 0:
        failed = True
        reasons.append("5xx")
    if latency_ms > p95_ms:
        failed = True
        reasons.append(f"latency_gt_{p95_ms}ms")

    if failed:
        return ScenarioResult(
            "api_smoke", "failed", ",".join(reasons), [str(log_path)]
        )
    return ScenarioResult(
        "api_smoke", "passed", f"status={status} latency_ms={latency_ms}", [str(log_path)]
    )


def _load_cfg(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config is not a mapping")
    return data


def _compute_ingest_signature(secret: str, body: bytes) -> str:
    """Compute HMAC-SHA256 for robot ingest S2S auth."""
    return hmac_mod.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Aspire Robot Regression Runner")
    ap.add_argument("--config", required=True, help="Path to robots.config.yaml")
    ap.add_argument(
        "--env",
        required=True,
        choices=["staging", "canary", "production"],
        help="Target environment",
    )
    ap.add_argument("--version-ref", required=True, help="Git SHA or version tag")
    ap.add_argument(
        "--post", action="store_true", help="Force POST to ingest endpoint"
    )
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = _load_cfg(cfg_path)

    # Project root is the repo root (two levels above the config in standard layout)
    project_root = Path.cwd()

    run_id = str(uuid.uuid4())
    out_dir = Path("robots/out")
    evidence_dir = out_dir / "evidence" / run_id
    _ensure_dir(evidence_dir)

    # Resolve schema relative to this script
    schema_path = Path(__file__).parent / "schemas" / "robot_run.schema.json"

    started = _now_iso()

    enabled = cfg.get("scenarios", {}).get("enabled", ["sync_validate"])
    results: list[ScenarioResult] = []

    if "sync_validate" in enabled:
        results.append(scenario_sync_validate(cfg, project_root))

    if cfg.get("mode", "validate") == "smoke":
        if "api_smoke" in enabled:
            results.append(scenario_api_smoke(cfg, args.env, evidence_dir))

    finished = _now_iso()

    overall = "failed" if any(r.status == "failed" for r in results) else "passed"

    robot_run: dict[str, Any] = {
        "id": run_id,
        "env": args.env,
        "suite": "aspire_robots",
        "status": overall,
        "startedAt": started,
        "finishedAt": finished,
        "versionRef": args.version_ref,
        "summary": "; ".join([f"{r.name}:{r.status}" for r in results]),
        "scenarios": [
            {
                "name": r.name,
                "status": r.status,
                "summary": r.summary,
                "evidence": r.evidence,
            }
            for r in results
        ],
        "meta": {
            "mode": cfg.get("mode", "validate"),
            "config": str(cfg_path),
        },
    }

    # Validate output against schema
    schema = _read_json(schema_path)
    js_validate(instance=robot_run, schema=schema)

    _ensure_dir(out_dir / "results")
    result_path = out_dir / "results" / f"{run_id}.json"
    result_path.write_text(json.dumps(robot_run, indent=2), encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps({"latest_run_id": run_id}, indent=2), encoding="utf-8"
    )

    # Optional ingest POST
    ingest_cfg = cfg.get("ingest", {})
    if (args.post or ingest_cfg.get("enabled")) and ingest_cfg.get("url"):
        url = ingest_cfg["url"]
        body_bytes = json.dumps(robot_run).encode("utf-8")
        headers: dict[str, str] = {"Content-Type": "application/json"}

        # S2S HMAC auth if secret is available
        secret = os.environ.get("ASPIRE_ROBOT_S2S_SECRET", "")
        if secret:
            sig = _compute_ingest_signature(secret, body_bytes)
            headers["X-Robot-Signature"] = sig

        try:
            resp = requests.post(url, headers=headers, data=body_bytes, timeout=15)
            (evidence_dir / "ingest_response.txt").write_text(
                f"{resp.status_code}\n{resp.text}\n", encoding="utf-8"
            )
        except Exception as e:
            (evidence_dir / "ingest_response.txt").write_text(
                f"ERROR: {e}\n", encoding="utf-8"
            )

    print(json.dumps(robot_run, indent=2))

    return 0 if overall == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
