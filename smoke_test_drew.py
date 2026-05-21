"""
Wave 9j smoke test — hits production Drew with the canonical golden fixture
and reports each stage with real timing + receipts. No agent guessing.

Usage:
  railway run --service Ava-Brain python smoke_test_drew.py

Required env (auto-injected by railway run):
  ASPIRE_TOKEN_SIGNING_KEY  - HMAC secret for capability token minting
  AVA_BRAIN_URL             - optional, defaults to production
  ASPIRE_SUPABASE_URL       - for receipt + project queries
  ASPIRE_SUPABASE_SERVICE_ROLE_KEY - same
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

FIXTURE_PATH = Path("orchestrator/tests/fixtures/blueprints/gavnn_addendum_1.pdf")
ORCH_URL = os.getenv("AVA_BRAIN_URL", "https://ava-brain-production.up.railway.app")
SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = SUITE_ID
POLL_INTERVAL_S = 5
MAX_WAIT_S = 30 * 60  # 30 min hard cap


def mint_token(scope: str, correlation_id: str) -> dict:
    """Replicates orchestrator/services/token_service.mint_token exactly."""
    signing_key = os.environ["ASPIRE_TOKEN_SIGNING_KEY"]
    token_id = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=45)
    payload = {
        "token_id": token_id,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "tool": "blueprints",
        "scopes": sorted([scope]),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "correlation_id": correlation_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    payload["signature"] = sig
    payload["revoked"] = False
    return payload


def post_dispatch(pdf_bytes: bytes, correlation_id: str) -> dict:
    token = mint_token("blueprints:upload", correlation_id)
    body = {
        "pdf_bytes": base64.b64encode(pdf_bytes).decode("ascii"),
        "filename": "gavnn_addendum_1.pdf",
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "correlation_id": correlation_id,
        "capability_token": json.dumps(token),
    }
    print(f"  → POST {ORCH_URL}/v1/blueprints/dispatch  ({len(pdf_bytes)/1024/1024:.1f} MB)")
    resp = httpx.post(
        f"{ORCH_URL}/v1/blueprints/dispatch",
        json=body,
        headers={"Authorization": f"Bearer {os.environ['ASPIRE_TOKEN_SIGNING_KEY'][:32]}"},
        timeout=90.0,
    )
    print(f"  ← {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def fetch_project_status(project_id: str, correlation_id: str) -> dict:
    token = mint_token("blueprints:read", correlation_id)
    resp = httpx.get(
        f"{ORCH_URL}/v1/blueprints/projects/{project_id}/status",
        headers={
            "Authorization": f"Bearer {os.environ['ASPIRE_TOKEN_SIGNING_KEY'][:32]}",
            "X-Capability-Token": json.dumps(token),
            "X-Suite-Id": SUITE_ID,
            "X-Office-Id": OFFICE_ID,
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        return {"_error": resp.status_code, "_body": resp.text[:200]}
    return resp.json()


def main() -> int:
    if not FIXTURE_PATH.exists():
        print(f"✗ Fixture missing: {FIXTURE_PATH}")
        return 1
    if "ASPIRE_TOKEN_SIGNING_KEY" not in os.environ:
        print("✗ ASPIRE_TOKEN_SIGNING_KEY not set. Run with: railway run --service Ava-Brain python smoke_test_drew.py")
        return 1

    pdf_bytes = FIXTURE_PATH.read_bytes()
    correlation_id = f"smoke-{uuid.uuid4()}"
    print(f"━━━ Wave 9j Smoke Test ━━━")
    print(f"Fixture:   {FIXTURE_PATH.name} ({len(pdf_bytes)/1024/1024:.1f} MB)")
    print(f"SHA-256:   {hashlib.sha256(pdf_bytes).hexdigest()[:16]}...")
    print(f"Target:    {ORCH_URL}")
    print(f"Corr ID:   {correlation_id}")
    print()
    print("[1] Dispatching to /v1/blueprints/dispatch ...")
    t0 = time.time()
    try:
        dispatch_resp = post_dispatch(pdf_bytes, correlation_id)
    except Exception as exc:
        print(f"✗ Dispatch failed: {type(exc).__name__}: {exc}")
        return 2
    project_id = dispatch_resp.get("project_id")
    status_initial = dispatch_resp.get("status")
    print(f"  project_id: {project_id}")
    print(f"  status:     {status_initial}  ({(time.time()-t0):.1f}s)")
    print()

    if status_initial == "dedup":
        print("→ Dedup hit. Project already exists. Polling its current state.")
    else:
        print("→ Pipeline enqueued. Polling stage_progress.")

    print()
    print("[2] Polling project status every {}s (max {} min) ...".format(POLL_INTERVAL_S, MAX_WAIT_S // 60))
    print()
    seen_states: dict[str, str] = {}
    last_print_t = 0.0
    poll_start = time.time()

    while time.time() - poll_start < MAX_WAIT_S:
        time.sleep(POLL_INTERVAL_S)
        status = fetch_project_status(project_id, correlation_id)
        if "_error" in status:
            print(f"  poll @{int(time.time()-poll_start)}s: {status['_error']} {status['_body']}")
            continue
        sp = status.get("stage_progress", {})
        elapsed = int(time.time() - poll_start)
        for stage in ("ingest", "classify", "see", "reason", "procure"):
            state = sp.get(stage, "?")
            prev = seen_states.get(stage)
            if state != prev:
                seen_states[stage] = state
                emoji = {"not_started": "·", "in_progress": "↻", "done": "✓", "failed": "✗"}.get(state, "?")
                print(f"  [{elapsed:>4}s] {emoji} {stage:<10} {prev or '·'} → {state}")
        # Terminal check
        if all(sp.get(s) in ("done", "failed", "stub") for s in ("ingest", "classify", "see", "reason", "procure")):
            print()
            print("━━━ Terminal state reached ━━━")
            failed = [s for s in sp if sp[s] == "failed"]
            done = [s for s in sp if sp[s] == "done"]
            print(f"  done:   {done}")
            print(f"  failed: {failed}")
            print(f"  total:  {elapsed}s")
            return 0 if not failed else 3
        if time.time() - last_print_t > 30:
            last_print_t = time.time()
            print(f"  [{elapsed:>4}s] still running: {sp}")

    print(f"✗ Timed out after {MAX_WAIT_S}s. Last state: {sp}")
    return 4


if __name__ == "__main__":
    sys.exit(main())
