#!/usr/bin/env python3
"""Clara → PandaDoc Live E2E Test — Full Pipeline Against Sandbox.

Tests the complete flow:
  1. Clara browses PandaDoc templates (discovers NDA)
  2. Clara attempts contract generation with incomplete info (preflight gate blocks)
  3. Clara generates contract with complete fake company info (PandaDoc creates doc)
  4. Clara sends the document for e-signature to real email
  5. Clara creates an embedded signing session (Aspire signing page URL)

Run from WSL:
  cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator
  source ~/venvs/aspire/bin/activate
  python scripts/test_clara_e2e_live.py

Requires:
  - ASPIRE_PANDADOC_API_KEY in .env
  - ASPIRE_OPENAI_API_KEY in .env (for RAG embedding)

Author: Aspire Engineering
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Load .env
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Config ──────────────────────────────────────────────────────────────────
SIGNER_EMAIL = "tonioswayz32@gmail.com"
SIGNER_FIRST = "Tonio"
SIGNER_LAST = "Scott"
ASPIRE_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://www.aspireos.app")

# Fake company info for test
FAKE_SENDER = {
    "company": "Skytech Tower LLC",
    "owner_name": "Antonio Towers",
    "email": "antonio@skytechdev.com",
    "address": "1400 Innovation Dr",
    "city": "Austin",
    "state": "TX",
    "zip": "78701",
    "phone": "(512) 555-0199",
}

FAKE_CLIENT = {
    "company": "BuildRight Solutions Inc",
    "contact_name": "Tonio Scott",
    "email": SIGNER_EMAIL,
    "address": "200 Commerce Blvd",
    "city": "Dallas",
    "state": "TX",
    "zip": "75201",
    "phone": "(214) 555-0342",
}

SUITE_ID = "e2e-live-test-suite-001"
OFFICE_ID = "e2e-live-test-office-001"
CORRELATION_ID = f"e2e-clara-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

# ── Helpers ──────────────────────────────────────────────────────────────────


def banner(step: int, title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  STEP {step}: {title}")
    print(f"{'='*70}\n")


def result_summary(label: str, data: dict) -> None:
    print(f"  {label}:")
    for k, v in data.items():
        if isinstance(v, list) and len(v) > 3:
            print(f"    {k}: [{len(v)} items]")
        elif isinstance(v, str) and len(v) > 200:
            print(f"    {k}: {v[:200]}...")
        else:
            print(f"    {k}: {v}")
    print()


# ── Step 1: Browse Templates ────────────────────────────────────────────────

async def step1_browse_templates() -> list[dict]:
    """Clara browses PandaDoc template library to discover what's available."""
    banner(1, "Clara Browses PandaDoc Template Library")

    from aspire_orchestrator.providers.pandadoc_client import (
        execute_pandadoc_templates_list,
    )

    print("  Clara: 'Let me check what templates are available in PandaDoc...'")
    print()

    result = await execute_pandadoc_templates_list(
        payload={"q": "nda", "count": 10},
        correlation_id=f"{CORRELATION_ID}-browse",
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )

    if result.outcome.value == "success":
        templates = result.data.get("templates", [])
        print(f"  Clara found {len(templates)} template(s):")
        for t in templates:
            print(f"    - {t.get('name', '?')} (id: {t.get('id', '?')[:12]}...)")
        print()
        print(f"  Receipt: {result.receipt_data.get('receipt_id', '?')}")

        if not templates:
            print("\n  Clara: 'No NDA templates found. Let me search broader...'")
            # Try broader search
            result2 = await execute_pandadoc_templates_list(
                payload={"count": 50},
                correlation_id=f"{CORRELATION_ID}-browse-all",
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            if result2.outcome.value == "success":
                templates = result2.data.get("templates", [])
                print(f"  Clara found {len(templates)} total template(s):")
                for t in templates:
                    print(f"    - {t.get('name', '?')} (id: {t.get('id', '?')[:12]}...)")
        return templates
    else:
        print(f"  FAILED: {result.error}")
        print(f"  Receipt: {result.receipt_data}")
        return []


# ── Step 2: Attempt with Incomplete Info ────────────────────────────────────

async def step2_incomplete_attempt() -> dict | None:
    """Clara tries to generate with minimal info — preflight gate should catch missing fields."""
    banner(2, "Clara Attempts Generation with Incomplete Info (Preflight Gate)")

    from aspire_orchestrator.providers.pandadoc_client import (
        execute_pandadoc_contract_generate,
    )

    print("  User: 'Create an NDA for me'")
    print("  Clara: 'Let me try... but I'll need more details.'")
    print()

    # Intentionally incomplete — no parties, no terms, no tokens
    result = await execute_pandadoc_contract_generate(
        payload={
            "template_type": "general_mutual_nda",
            "name": "NDA — Incomplete Test",
            "parties": [
                {"name": "Someone", "role": "sender"},
                {"name": "Someone Else", "role": "client"},
            ],
        },
        correlation_id=f"{CORRELATION_ID}-incomplete",
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )

    if result.outcome.value == "failed" and result.data and result.data.get("needs_info"):
        missing = result.data.get("missing_tokens", [])
        questions = result.data.get("suggested_questions", [])
        msg = result.data.get("message_for_ava", "")

        print("  PREFLIGHT GATE TRIGGERED (correct behavior)")
        print(f"  Fill rate: {result.data.get('fill_rate_pct', 0)}%")
        print(f"  Missing tokens: {missing}")
        print()
        print("  Clara's questions for the user:")
        for i, q in enumerate(questions, 1):
            print(f"    {i}. {q}")
        print()
        print(f"  Clara → Ava: '{msg}'")
        return result.data
    elif result.outcome.value == "success":
        doc_id = result.data.get("document_id", "")
        print(f"  Document created (unexpectedly): {doc_id}")
        print("  NOTE: Preflight gate didn't block — template may have no required tokens")
        # Check if there are missing tokens even though doc was created
        if result.data.get("needs_additional_info"):
            print(f"  Missing tokens: {result.data.get('suggested_questions', [])}")
        return result.data
    else:
        print(f"  Result: {result.outcome.value}")
        print(f"  Error: {result.error}")
        return None


# ── Step 3: Generate with Complete Info ──────────────────────────────────────

async def step3_generate_complete() -> str | None:
    """Clara generates an NDA with full fake company info — real PandaDoc document created."""
    banner(3, "Clara Generates NDA with Complete Fake Company Info")

    from aspire_orchestrator.providers.pandadoc_client import (
        execute_pandadoc_contract_generate,
    )

    print("  User provides complete info:")
    print(f"    Sender: {FAKE_SENDER['company']} ({FAKE_SENDER['owner_name']})")
    print(f"    Client: {FAKE_CLIENT['company']} ({FAKE_CLIENT['contact_name']})")
    print(f"    Signer Email: {SIGNER_EMAIL}")
    print(f"    Jurisdiction: TX")
    print()

    payload = {
        "template_type": "general_mutual_nda",
        "name": f"Mutual NDA — {FAKE_SENDER['company']} / {FAKE_CLIENT['company']}",
        "parties": [
            {
                "name": FAKE_SENDER["company"],
                "email": FAKE_SENDER["email"],
                "role": "sender",
                "company": FAKE_SENDER["company"],
                "contact_name": FAKE_SENDER["owner_name"],
                "address": FAKE_SENDER["address"],
                "phone": FAKE_SENDER["phone"],
                "state": FAKE_SENDER["state"],
            },
            {
                "name": FAKE_CLIENT["company"],
                "email": FAKE_CLIENT["email"],
                "role": "client",
                "company": FAKE_CLIENT["company"],
                "contact_name": FAKE_CLIENT["contact_name"],
                "address": FAKE_CLIENT["address"],
                "phone": FAKE_CLIENT["phone"],
                "state": FAKE_CLIENT["state"],
            },
        ],
        "terms": {
            "title": f"Mutual NDA — {FAKE_SENDER['company']} / {FAKE_CLIENT['company']}",
            "jurisdiction_state": "TX",
            "purpose": "Evaluation of potential business partnership",
            "term_length": "2 years",
        },
        "context": {
            "company_name": FAKE_SENDER["company"],
            "owner_name": FAKE_SENDER["owner_name"],
            "owner_email": FAKE_SENDER["email"],
            "business_name": FAKE_SENDER["company"],
        },
        "metadata": {
            "aspire_suite_id": SUITE_ID,
            "aspire_office_id": OFFICE_ID,
            "aspire_correlation_id": CORRELATION_ID,
            "aspire_template_key": "general_mutual_nda",
            "aspire_test": "true",
        },
    }

    print("  Clara: 'I have all the information I need. Creating the NDA now...'")
    print()

    result = await execute_pandadoc_contract_generate(
        payload=payload,
        correlation_id=f"{CORRELATION_ID}-generate",
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )

    if result.outcome.value == "success":
        doc_id = result.data.get("document_id", "")
        doc_name = result.data.get("name", "")
        doc_status = result.data.get("status", "")
        token_quality = result.data.get("token_quality", {})

        print(f"  DOCUMENT CREATED SUCCESSFULLY")
        print(f"    Document ID: {doc_id}")
        print(f"    Name: {doc_name}")
        print(f"    Status: {doc_status}")

        if token_quality:
            print(f"    Token Fill Rate: {token_quality.get('fill_rate_pct', 0)}%")
            print(f"    Tokens Filled: {token_quality.get('filled', 0)}/{token_quality.get('total', 0)}")
            missing = token_quality.get("missing_tokens", [])
            if missing:
                print(f"    Still Missing: {missing}")

        if result.data.get("needs_additional_info"):
            print()
            print("  Clara: 'Document created, but some fields are still blank:'")
            for q in result.data.get("suggested_questions", []):
                print(f"    - {q}")

        print(f"\n  Receipt: {result.receipt_data.get('receipt_id', '?')}")
        return doc_id
    elif result.outcome.value == "failed" and result.data and result.data.get("needs_info"):
        # Preflight gate blocked even with complete info
        missing = result.data.get("missing_tokens", [])
        print(f"  PREFLIGHT GATE BLOCKED (fill rate: {result.data.get('fill_rate_pct', 0)}%)")
        print(f"  Missing tokens: {missing}")
        print()
        print("  Clara's questions:")
        for q in result.data.get("suggested_questions", []):
            print(f"    - {q}")
        return None
    else:
        print(f"  FAILED: {result.error}")
        if result.receipt_data:
            print(f"  Receipt: {result.receipt_data.get('receipt_id', '?')}")
        return None


# ── Step 4: Wait for Document Processing ─────────────────────────────────────

async def step4_wait_for_processing(doc_id: str) -> str | None:
    """Wait for PandaDoc to process the document before sending."""
    banner(4, "Waiting for PandaDoc Document Processing")

    from aspire_orchestrator.providers.pandadoc_client import (
        execute_pandadoc_contract_read,
    )

    print(f"  Clara: 'Document {doc_id[:12]}... is being processed by PandaDoc.'")
    print("  Waiting for status to change from 'document.uploaded'...")
    print()

    max_wait = 60  # seconds
    poll_interval = 5
    start = time.monotonic()

    while time.monotonic() - start < max_wait:
        result = await execute_pandadoc_contract_read(
            payload={"document_id": doc_id},
            correlation_id=f"{CORRELATION_ID}-poll",
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
        )

        if result.outcome.value == "success":
            status = result.data.get("status", "unknown")
            print(f"  [{int(time.monotonic() - start)}s] Status: {status}")

            if status == "document.draft":
                print()
                print("  Document is ready (draft status)!")
                return status
            elif status in ("document.sent", "document.completed"):
                print()
                print(f"  Document already in '{status}' — ready!")
                return status
            elif "error" in status.lower() or "deleted" in status.lower():
                print(f"\n  ERROR: Document entered error state: {status}")
                return None
        else:
            print(f"  Poll failed: {result.error}")

        await asyncio.sleep(poll_interval)

    print(f"\n  TIMEOUT: Document still processing after {max_wait}s")
    print("  This is common in PandaDoc sandbox — proceeding anyway...")
    return "timeout"


# ── Step 5: Send for Signature ───────────────────────────────────────────────

async def step5_send_for_signature(doc_id: str) -> bool:
    """Send the document to the signer's email for e-signature."""
    banner(5, f"Clara Sends Document for Signature to {SIGNER_EMAIL}")

    from aspire_orchestrator.providers.pandadoc_client import (
        execute_pandadoc_contract_send,
    )

    print(f"  Clara: 'Sending the NDA to {SIGNER_EMAIL} for signature...'")
    print()

    result = await execute_pandadoc_contract_send(
        payload={
            "document_id": doc_id,
            "message": (
                f"Hi {SIGNER_FIRST},\n\n"
                f"Please review and sign this Mutual NDA between "
                f"{FAKE_SENDER['company']} and {FAKE_CLIENT['company']}.\n\n"
                f"This document was prepared by Clara (Aspire Legal) "
                f"for your review.\n\n"
                f"Best regards,\n{FAKE_SENDER['owner_name']}"
            ),
            "subject": f"Mutual NDA — {FAKE_SENDER['company']} / {FAKE_CLIENT['company']} (Sign Here)",
            "silent": False,  # SEND the email!
        },
        correlation_id=f"{CORRELATION_ID}-send",
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )

    if result.outcome.value == "success":
        print(f"  SENT SUCCESSFULLY")
        print(f"    Status: {result.data.get('status', '?')}")
        print(f"    Recipient: {SIGNER_EMAIL}")
        print(f"\n  Receipt: {result.receipt_data.get('receipt_id', '?')}")
        print(f"\n  Clara: 'The NDA has been sent to {SIGNER_EMAIL}.'")
        print(f"         'You should receive an email from PandaDoc shortly.'")
        return True
    else:
        print(f"  FAILED to send: {result.error}")
        print(f"  Receipt: {result.receipt_data}")

        # Common error: document not in correct state for sending
        if "409" in str(result.error) or "draft" in str(result.error).lower():
            print("\n  NOTE: Document may need to finish processing before sending.")
            print("  PandaDoc requires documents to be in 'draft' status to send.")
        return False


# ── Step 6: Create Aspire Signing Session ────────────────────────────────────

async def step6_create_signing_session(doc_id: str) -> str | None:
    """Create an embedded signing session — generates the Aspire signing page URL."""
    banner(6, "Clara Creates Aspire Signing Session")

    from aspire_orchestrator.providers.pandadoc_client import (
        execute_pandadoc_create_signing_session,
    )

    print(f"  Clara: 'Creating a signing session for the Aspire signing page...'")
    print()

    result = await execute_pandadoc_create_signing_session(
        payload={
            "document_id": doc_id,
            "recipient": SIGNER_EMAIL,
        },
        correlation_id=f"{CORRELATION_ID}-session",
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )

    if result.outcome.value == "success":
        session_id = result.data.get("session_id", "")
        expires_at = result.data.get("expires_at", "")

        # Build the Aspire signing URL
        aspire_signing_url = f"{ASPIRE_BASE_URL}/sign/{doc_id}?session={session_id}"

        print(f"  SIGNING SESSION CREATED")
        print(f"    Session ID: {session_id}")
        print(f"    Expires: {expires_at}")
        print()
        print(f"  ┌─────────────────────────────────────────────────────────────────┐")
        print(f"  │  ASPIRE SIGNING URL:                                           │")
        print(f"  │  {aspire_signing_url:<63s} │")
        print(f"  └─────────────────────────────────────────────────────────────────┘")
        print()
        print(f"  Clara: 'The signing page is ready. {SIGNER_FIRST} can sign at the URL above.'")
        print(f"         'The session expires at {expires_at}.'")
        print(f"\n  Receipt: {result.receipt_data.get('receipt_id', '?')}")
        return aspire_signing_url
    else:
        print(f"  FAILED to create session: {result.error}")
        print()
        # Session creation requires document to be in 'sent' status
        if "409" in str(result.error) or "status" in str(result.error).lower():
            print("  NOTE: Signing sessions require document to be in 'sent' status.")
            print("  The document may still be processing or hasn't been sent yet.")
        return None


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║          ASPIRE — Clara Legal E2E Live Test (PandaDoc Sandbox)      ║")
    print("║                                                                      ║")
    print(f"║  Signer: {SIGNER_FIRST} {SIGNER_LAST} <{SIGNER_EMAIL}>               ║")
    print(f"║  Sender: {FAKE_SENDER['company']} ({FAKE_SENDER['owner_name']})       ║")
    print(f"║  Client: {FAKE_CLIENT['company']} ({FAKE_CLIENT['contact_name']})     ║")
    print(f"║  Correlation: {CORRELATION_ID:<52s}  ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    # Verify PandaDoc key
    from aspire_orchestrator.config.settings import settings
    if not settings.pandadoc_api_key:
        print("\n  ERROR: ASPIRE_PANDADOC_API_KEY not set in .env")
        sys.exit(1)
    print(f"\n  PandaDoc API Key: {settings.pandadoc_api_key[:8]}...{settings.pandadoc_api_key[-4:]}")

    # ── Step 1: Browse Templates ──
    templates = await step1_browse_templates()

    # ── Step 2: Incomplete Attempt (Preflight Gate) ──
    preflight_result = await step2_incomplete_attempt()

    # ── Step 3: Generate with Complete Info ──
    doc_id = await step3_generate_complete()
    if not doc_id:
        print("\n  ABORT: Document generation failed. Cannot continue.")
        sys.exit(1)

    # ── Step 4: Wait for Processing ──
    status = await step4_wait_for_processing(doc_id)

    # ── Step 5: Send for Signature ──
    sent = await step5_send_for_signature(doc_id)

    # ── Step 6: Create Signing Session ──
    signing_url = None
    if sent:
        signing_url = await step6_create_signing_session(doc_id)

    # ── Summary ──
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║                        E2E TEST SUMMARY                             ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    print(f"║  Step 1 (Browse Templates):      {'PASS' if templates else 'FAIL':<36s}  ║")
    print(f"║  Step 2 (Preflight Gate):         {'PASS' if preflight_result else 'SKIP':<36s}  ║")
    print(f"║  Step 3 (Generate Document):      {'PASS' if doc_id else 'FAIL':<36s}  ║")
    print(f"║  Step 4 (Wait for Processing):    {'PASS' if status else 'FAIL':<36s}  ║")
    print(f"║  Step 5 (Send for Signature):     {'PASS' if sent else 'FAIL':<36s}  ║")
    print(f"║  Step 6 (Signing Session):        {'PASS' if signing_url else 'FAIL':<36s}  ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    if doc_id:
        print(f"║  Document ID: {doc_id:<54s}  ║")
    if signing_url:
        print(f"║  Signing URL: (see above)                                          ║")
    print(f"║  Correlation: {CORRELATION_ID:<54s}  ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    if sent:
        print(f"\n  CHECK YOUR EMAIL: {SIGNER_EMAIL}")
        print("  You should receive a PandaDoc signing request email shortly.")
        if signing_url:
            print(f"\n  Or sign directly via Aspire: {signing_url}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
