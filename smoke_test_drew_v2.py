"""
Wave 10 smoke test — 3-step upload flow (upload-init → PUT to Storage → upload-complete).

The old smoke_test_drew.py POSTed 22 MB base64 JSON to /dispatch — Railway proxy
killed the connection mid-upload. Production flow uploads directly to Supabase
Storage via pre-signed PUT, then calls upload-complete with just the upload_id.

Usage (with env vars exported):
  python smoke_test_drew_v2.py
"""
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

import os as _os_fixture
FIXTURE_PATH = Path(_os_fixture.getenv(
    "SMOKE_FIXTURE",
    "orchestrator/tests/fixtures/blueprints/gavnn_addendum_1.pdf",
))
ORCH_URL = os.getenv("AVA_BRAIN_URL", "https://ava-brain-production.up.railway.app")
SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = SUITE_ID
POLL_INTERVAL_S = 5
MAX_WAIT_S = 30 * 60


def mint_token(scope: str, correlation_id: str) -> dict:
    signing_key = os.environ["ASPIRE_TOKEN_SIGNING_KEY"]
    payload = {
        "token_id": str(uuid.uuid4()),
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "tool": "blueprints",
        "scopes": sorted([scope]),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=45)).isoformat(),
        "correlation_id": correlation_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(signing_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    payload["signature"] = sig
    payload["revoked"] = False
    return payload


def main() -> int:
    if not FIXTURE_PATH.exists():
        print(f"FIXTURE MISSING: {FIXTURE_PATH}")
        return 1

    pdf_bytes = FIXTURE_PATH.read_bytes()
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    correlation_id = f"smoke10-{uuid.uuid4()}"

    print(f"Wave 10 smoke test (Modal GPU SEE)")
    print(f"  fixture:  {FIXTURE_PATH.name} ({len(pdf_bytes)/1024/1024:.1f} MB)")
    print(f"  sha256:   {pdf_sha[:16]}...")
    print(f"  corr_id:  {correlation_id}")
    print(f"  target:   {ORCH_URL}")
    print()

    t_overall = time.monotonic()

    # ---- Step 1: upload-init ----
    print(f"[1] upload-init  @ {time.strftime('%H:%M:%S')}")
    init_token = mint_token("blueprints:upload_init", correlation_id)
    auth_hdr = {"Authorization": f"Bearer {os.environ['ASPIRE_TOKEN_SIGNING_KEY'][:32]}"}
    r = httpx.post(
        f"{ORCH_URL}/v1/blueprints/upload-init",
        headers=auth_hdr,
        json={
            "filename": FIXTURE_PATH.name,
            "size_bytes": len(pdf_bytes),
            "mime_type": "application/pdf",
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": correlation_id,
            "capability_token": json.dumps(init_token),
        },
        timeout=30.0,
    )
    print(f"    -> {r.status_code}")
    if r.status_code != 200:
        print(f"    body: {r.text[:500]}")
        return 1
    init = r.json()
    upload_id = init["upload_id"]
    signed_url = init["signed_url"]
    storage_path = init["storage_path"]
    print(f"    upload_id: {upload_id}")
    print(f"    storage_path: {storage_path}")

    # ---- Step 2: PUT raw bytes to Supabase Storage signed URL ----
    print(f"[2] PUT to Storage  @ {time.strftime('%H:%M:%S')}")
    t_put = time.monotonic()
    put_r = httpx.put(
        signed_url,
        content=pdf_bytes,
        headers={"Content-Type": "application/pdf"},
        timeout=120.0,
    )
    print(f"    -> {put_r.status_code} ({time.monotonic() - t_put:.1f}s)")
    if put_r.status_code not in (200, 201):
        print(f"    body: {put_r.text[:500]}")
        return 1

    # ---- Step 3: upload-complete ----
    print(f"[3] upload-complete  @ {time.strftime('%H:%M:%S')}")
    complete_token = mint_token("blueprints:upload_complete", correlation_id)
    r = httpx.post(
        f"{ORCH_URL}/v1/blueprints/upload-complete",
        headers=auth_hdr,
        json={
            "upload_id": upload_id,
            "filename": FIXTURE_PATH.name,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "correlation_id": correlation_id,
            "capability_token": json.dumps(complete_token),
        },
        timeout=60.0,
    )
    print(f"    -> {r.status_code}")
    if r.status_code not in (200, 202):
        print(f"    body: {r.text[:500]}")
        return 1
    body = r.json()
    project_id = body.get("project_id")
    initial_status = body.get("status")
    print(f"    project_id: {project_id}  status: {initial_status}")
    print(f"    pipeline enqueued at +{time.monotonic() - t_overall:.1f}s")

    # ---- Step 4: poll status ----
    print()
    print(f"[4] polling status every {POLL_INTERVAL_S}s")
    deadline = time.monotonic() + MAX_WAIT_S
    last_summary = ""
    while time.monotonic() < deadline:
        status_token = mint_token("blueprints:read", correlation_id)
        sr = httpx.get(
            f"{ORCH_URL}/v1/blueprints/projects/{project_id}/status",
            headers={"Authorization": f"Bearer {os.environ['ASPIRE_TOKEN_SIGNING_KEY'][:32]}",
                     "X-Capability-Token": json.dumps(status_token),
                     # Desktop server pattern (routes.ts): tenantId = suiteId for blueprint routes.
                     "X-Tenant-Id": SUITE_ID,
                     "X-Suite-Id": SUITE_ID, "X-Office-Id": OFFICE_ID},
            timeout=30.0,
        )
        if sr.status_code != 200:
            print(f"  poll {sr.status_code}: {sr.text[:200]}")
            time.sleep(POLL_INTERVAL_S)
            continue
        s = sr.json()
        elapsed = time.monotonic() - t_overall
        stages = s.get("stages") or {}
        summary = f"+{elapsed:5.1f}s  overall={s.get('overall_status','?')}  " + " ".join(
            f"{k}={v.get('status','?') if isinstance(v,dict) else v}" for k,v in stages.items()
        )
        if summary != last_summary:
            print(f"  {summary}")
            last_summary = summary
        if s.get("overall_status") in ("completed", "failed", "succeeded"):
            print(f"\nDONE at +{elapsed:.1f}s — overall={s.get('overall_status')}")
            print(json.dumps(s, indent=2)[:2000])
            return 0
        time.sleep(POLL_INTERVAL_S)

    print(f"\nTIMEOUT after {MAX_WAIT_S}s")
    return 2


if __name__ == "__main__":
    sys.exit(main())
