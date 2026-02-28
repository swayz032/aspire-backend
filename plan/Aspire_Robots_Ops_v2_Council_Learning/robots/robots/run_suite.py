#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
import requests
from jsonschema import validate as js_validate


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _now_iso() -> str:
    # RFC3339-ish
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _run_cmd(cmd: str, cwd: Path, timeout_s: int = 1200) -> Tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_s,
        text=True,
    )
    return proc.returncode, proc.stdout


@dataclass
class ScenarioResult:
    name: str
    status: str
    summary: str
    evidence: List[str]


def scenario_sync_validate(cfg: Dict[str, Any]) -> ScenarioResult:
    paths = cfg['paths']
    sent = cfg['sentinels']

    failures: List[str] = []
    checked: List[str] = []

    def check(root_key: str, root_path_key: str, sentinel_key: str) -> None:
        root = Path(paths[root_path_key]).resolve()
        if not root.exists():
            failures.append(f"{root_key} root missing: {root}")
            return
        for rel in sent[sentinel_key]:
            fp = root / rel
            checked.append(str(fp))
            if not fp.exists():
                failures.append(f"missing {root_key} sentinel: {rel}")

    check('admin_portal', 'admin_portal_root', 'admin_portal')
    check('expo_app', 'expo_app_root', 'expo_app')
    check('roadmap', 'roadmap_root', 'roadmap')
    check('trustspine', 'trustspine_root', 'trustspine')

    if failures:
        return ScenarioResult(
            name='sync_validate',
            status='failed',
            summary='; '.join(failures[:10]),
            evidence=[],
        )

    return ScenarioResult(
        name='sync_validate',
        status='passed',
        summary=f"sentinels OK ({len(checked)} files checked)",
        evidence=[],
    )


def scenario_repo_health(cfg: Dict[str, Any], evidence_dir: Path) -> ScenarioResult:
    paths = cfg['paths']
    cmds = cfg['scenarios']['repo_health']

    ev: List[str] = []
    failures: List[str] = []

    for key, root_key, cmd_key in [
        ('admin', 'admin_portal_root', 'admin_cmd'),
        ('expo', 'expo_app_root', 'expo_cmd')
    ]:
        root = Path(paths[root_key]).resolve()
        log_path = evidence_dir / f"repo_health_{key}.log"
        if not root.exists():
            failures.append(f"{key} root missing")
            continue
        rc, out = _run_cmd(cmds.get(cmd_key, ''), cwd=root)
        log_path.write_text(out, encoding='utf-8')
        ev.append(str(log_path))
        if rc != 0:
            failures.append(f"{key} cmd failed (rc={rc})")

    if failures:
        return ScenarioResult('repo_health', 'failed', '; '.join(failures), ev)
    return ScenarioResult('repo_health', 'passed', 'repo commands OK', ev)


def scenario_api_smoke(cfg: Dict[str, Any], env: str, evidence_dir: Path) -> ScenarioResult:
    env_defaults = cfg['env_defaults'][env]
    base_url = env_defaults['base_url'].rstrip('/')
    s_cfg = cfg['scenarios']['api_smoke']
    health_path = s_cfg.get('health_path', '/health')
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

    log_path = evidence_dir / 'api_smoke.log'
    log_path.write_text(f"URL: {url}\nstatus: {status}\nlatency_ms: {latency_ms}\nbody:\n{body}\n", encoding='utf-8')

    p95_ms = int(s_cfg.get('p95_ms', 800))
    max_5xx = int(s_cfg.get('max_5xx', 0))

    failed = False
    reasons = []
    if status == 0:
        failed = True
        reasons.append('request_failed')
    if status >= 500 and max_5xx == 0:
        failed = True
        reasons.append('5xx')
    if latency_ms > p95_ms:
        failed = True
        reasons.append(f"latency_gt_{p95_ms}ms")

    if failed:
        return ScenarioResult('api_smoke', 'failed', ','.join(reasons), [str(log_path)])
    return ScenarioResult('api_smoke', 'passed', f"status={status} latency_ms={latency_ms}", [str(log_path)])


def _load_cfg(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('config is not a mapping')
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--env', required=True, choices=['staging', 'canary', 'production'])
    ap.add_argument('--version-ref', required=True)
    ap.add_argument('--post', action='store_true', help='force POST to ingest if configured')
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = _load_cfg(cfg_path)

    run_id = str(uuid.uuid4())
    out_dir = Path('robots/out')
    evidence_dir = out_dir / 'evidence' / run_id
    _ensure_dir(evidence_dir)

    started = _now_iso()

    enabled = cfg.get('scenarios', {}).get('enabled', ['sync_validate'])
    results: List[ScenarioResult] = []

    # Always run sync_validate if present
    if 'sync_validate' in enabled:
        results.append(scenario_sync_validate(cfg))

    if cfg.get('mode', 'validate') == 'smoke':
        if 'repo_health' in enabled:
            results.append(scenario_repo_health(cfg, evidence_dir))
        if 'api_smoke' in enabled:
            results.append(scenario_api_smoke(cfg, args.env, evidence_dir))
        # ui_smoke is scaffolded but optional; not executed here by default to avoid heavy deps.

    finished = _now_iso()

    # Derive overall status
    if any(r.status == 'failed' for r in results):
        overall = 'failed'
    else:
        overall = 'passed'

    robot_run = {
        'id': run_id,
        'env': args.env,
        'suite': 'aspire_robots',
        'status': overall,
        'startedAt': started,
        'finishedAt': finished,
        'versionRef': args.version_ref,
        'summary': '; '.join([f"{r.name}:{r.status}" for r in results]),
        'scenarios': [
            {
                'name': r.name,
                'status': r.status,
                'summary': r.summary,
                'evidence': r.evidence,
            }
            for r in results
        ],
        'meta': {
            'mode': cfg.get('mode', 'validate'),
            'config': str(cfg_path),
        },
    }

    # Validate payload
    schema = _read_json(Path('robots/schemas/robot_run.schema.json'))
    js_validate(instance=robot_run, schema=schema)

    _ensure_dir(out_dir / 'results')
    (out_dir / 'results' / f"{run_id}.json").write_text(json.dumps(robot_run, indent=2), encoding='utf-8')
    (out_dir / 'manifest.json').write_text(json.dumps({'latest_run_id': run_id}, indent=2), encoding='utf-8')

    # Optional ingest
    ingest_cfg = cfg.get('ingest', {})
    if (args.post or ingest_cfg.get('enabled')) and ingest_cfg.get('url'):
        url = ingest_cfg['url']
        token = ingest_cfg.get('token', '')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f"Bearer {token}"
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(robot_run), timeout=15)
            (evidence_dir / 'ingest_response.txt').write_text(f"{resp.status_code}\n{resp.text}\n", encoding='utf-8')
        except Exception as e:
            (evidence_dir / 'ingest_response.txt').write_text(f"ERROR: {e}\n", encoding='utf-8')

    print(json.dumps(robot_run, indent=2))

    return 0 if overall == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
