#!/usr/bin/env python3
"""PandaDoc Sandbox Setup — Template discovery and registry UUID population.

Creates test documents in PandaDoc sandbox and maps template UUIDs into
the Aspire template registry (template_registry.json).

Usage:
    ASPIRE_PANDADOC_API_KEY=e7d42f15... python scripts/pandadoc_sandbox_setup.py

Auth: API-Key header (NOT Bearer — per PandaDoc API docs).
Rate limit: 10 req/min on sandbox — script enforces 7s between requests.

Steps:
    1. Verify API key connectivity
    2. List existing templates (if any)
    3. Create a test document to verify API integration
    4. Print template UUID mapping instructions
    5. Optionally update template_registry.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx required. Run: pip install httpx")
    sys.exit(1)

API_KEY = os.environ.get("ASPIRE_PANDADOC_API_KEY", "")
BASE_URL = "https://api.pandadoc.com/public/v1"
REGISTRY_PATH = Path(__file__).resolve().parent.parent / (
    "backend/orchestrator/src/aspire_orchestrator/config/template_registry.json"
)

# Sandbox rate limit: 10 req/min → 7s between requests (with margin)
_last_request = 0.0


def _rate_limit() -> None:
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < 7.0 and _last_request > 0:
        time.sleep(7.0 - elapsed)
    _last_request = time.monotonic()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"API-Key {API_KEY}",
        "Content-Type": "application/json",
    }


def verify_api_key() -> bool:
    """Verify PandaDoc API key is valid."""
    _rate_limit()
    resp = httpx.get(
        f"{BASE_URL}/documents", headers=_headers(),
        params={"count": 1}, timeout=15,
    )
    if resp.status_code == 200:
        print("   OK — API key is valid")
        return True
    print(f"   FAILED: HTTP {resp.status_code}")
    if resp.status_code == 401:
        print("   Check: Are you using API-Key auth (not Bearer)?")
    return False


def list_templates() -> list[dict]:
    """List existing templates in PandaDoc account."""
    _rate_limit()
    resp = httpx.get(
        f"{BASE_URL}/templates", headers=_headers(),
        params={"count": 50}, timeout=15,
    )
    if resp.status_code != 200:
        print(f"   Could not list templates: HTTP {resp.status_code}")
        return []

    data = resp.json()
    results = data.get("results", [])
    if results:
        print(f"   Found {len(results)} templates:")
        for t in results:
            print(f"     - {t.get('name', '?')} → UUID: {t.get('id', '?')}")
    else:
        print("   No templates found. Create them in the PandaDoc dashboard.")
    return results


def create_test_document() -> str:
    """Create a test document to verify API integration. Returns document ID."""
    _rate_limit()
    doc_body = {
        "name": "Aspire — Mutual NDA (E2E Test)",
        "recipients": [
            {
                "email": "party-a@aspire-test.com",
                "first_name": "Acme",
                "last_name": "Corp",
                "role": "signer",
            },
            {
                "email": "party-b@aspire-test.com",
                "first_name": "Wayne",
                "last_name": "Enterprises",
                "role": "signer",
            },
        ],
        "metadata": {
            "aspire_suite_id": "setup-test",
            "aspire_office_id": "setup-office",
            "aspire_template_key": "general_mutual_nda",
            "aspire_correlation_id": "setup-corr-001",
            "aspire_test": "true",
        },
        "tags": ["aspire-setup", "nda-template-test"],
    }

    resp = httpx.post(
        f"{BASE_URL}/documents", headers=_headers(),
        json=doc_body, timeout=15,
    )
    if resp.status_code in (200, 201):
        doc = resp.json()
        doc_id = doc.get("id", doc.get("uuid", ""))
        print(f"   OK — Document created: {doc_id}")
        print(f"   Status: {doc.get('status', '?')}")
        return doc_id
    else:
        print(f"   FAILED: HTTP {resp.status_code} — {resp.text[:200]}")
        return ""


def update_registry(template_key: str, template_uuid: str) -> None:
    """Update template_registry.json with a PandaDoc template UUID."""
    if not REGISTRY_PATH.exists():
        print(f"   Registry not found at {REGISTRY_PATH}")
        return

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    templates = registry.get("templates", {})

    if template_key in templates:
        templates[template_key]["pandadoc_template_uuid"] = template_uuid
        REGISTRY_PATH.write_text(
            json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"   Updated: {template_key} -> {template_uuid}")
    else:
        print(f"   Template key '{template_key}' not found in registry")


def main() -> None:
    if not API_KEY:
        print("ERROR: Set ASPIRE_PANDADOC_API_KEY environment variable")
        print("  Sandbox key: e7d42f15fad040c428ddcf4962b793a4ca6a9247")
        sys.exit(1)

    print("=" * 60)
    print("PandaDoc Sandbox Setup")
    print(f"  API Key: {API_KEY[:8]}...{API_KEY[-4:]}")
    print(f"  Registry: {REGISTRY_PATH}")
    print("=" * 60)

    # Step 1: Verify API key
    print("\n1. Verifying API key...")
    if not verify_api_key():
        sys.exit(1)

    # Step 2: List existing templates
    print("\n2. Listing existing templates...")
    templates = list_templates()

    # Step 3: Check for existing NDA
    nda_uuid = ""
    for t in templates:
        name = (t.get("name") or "").lower()
        if "nda" in name or "mutual" in name:
            nda_uuid = t.get("id", "")
            print(f"\n   Found existing NDA template: {nda_uuid}")
            break

    # Step 4: Create test document
    print("\n3. Creating test document...")
    doc_id = create_test_document()

    # Step 5: Update registry if template UUID available
    if nda_uuid:
        print(f"\n4. NDA Template UUID: {nda_uuid}")
        try:
            update_input = input("   Update template_registry.json? [y/N]: ").strip().lower()
        except EOFError:
            update_input = "n"
        if update_input == "y":
            update_registry("general_mutual_nda", nda_uuid)
            update_registry("general_one_way_nda", nda_uuid)

    # Summary
    print("\n" + "=" * 60)
    print("NEXT STEPS:")
    print("  1. Log into PandaDoc sandbox dashboard")
    print("  2. Create templates for each lane:")
    print("     - Trades: MSA-lite, SOW, Work Order, etc.")
    print("     - Accounting: Engagement Letter, Scope Addendum, etc.")
    print("     - Landlord: Residential Lease, Renewal, etc.")
    print("     - General: Mutual NDA, One-Way NDA")
    print("  3. Copy each template UUID")
    print("  4. Update backend/.../config/template_registry.json")
    print("  5. Run E2E test: pytest tests/test_pandadoc_e2e.py -v")
    if doc_id:
        print(f"\n  Test document ID: {doc_id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
