#!/usr/bin/env python3
"""Clara Send + Sign E2E — Uses PandaDoc org email to bypass sandbox restriction.

Creates an NDA, sends it for signature using the PandaDoc org email (owner@aspireos.app),
creates an embedded signing session, and outputs the Aspire signing URL.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def main() -> None:
    from aspire_orchestrator.providers.pandadoc_client import (
        execute_pandadoc_contract_generate,
        execute_pandadoc_contract_send,
        execute_pandadoc_create_signing_session,
    )
    import httpx

    corr = "e2e-clara-sign-flow"
    suite = "e2e-live-test-suite-001"
    office = "e2e-live-test-office-001"
    api_key = os.getenv("ASPIRE_PANDADOC_API_KEY", "")
    org_email = "owner@aspireos.app"

    print("=" * 70)
    print("  STEP 1: Create NDA with org email as recipient")
    print("=" * 70)

    # Use template_id directly (PandaDoc UUID from browsing)
    # and provide tokens as list of {name, value} dicts (highest priority override)
    nda_template_id = "aVPGZtb2PCBxvrZokgeRri"

    gen_result = await execute_pandadoc_contract_generate(
        payload={
            "template_id": nda_template_id,
            "name": "[DEV] Mutual NDA — Skytech Tower LLC / Aspire Org Test",
            "tokens": [
                {"name": "Sender.FirstName", "value": "Antonio"},
                {"name": "Sender.LastName", "value": "Towers"},
                {"name": "Sender.Company", "value": "Skytech Tower LLC"},
                {"name": "Sender.State", "value": "TX"},
                {"name": "Client.FirstName", "value": "Tonio"},
                {"name": "Client.LastName", "value": "Scott"},
                {"name": "Client.Company", "value": "Aspire HQ"},
            ],
            "parties": [
                {
                    "role": "Sender",
                    "name": "Antonio Towers",
                    "first_name": "Antonio",
                    "last_name": "Towers",
                    "email": org_email,
                    "company": "Skytech Tower LLC",
                },
                {
                    "role": "Client",
                    "name": "Tonio Scott",
                    "first_name": "Tonio",
                    "last_name": "Scott",
                    "email": org_email,
                    "company": "Aspire HQ",
                },
            ],
        },
        correlation_id=corr,
        suite_id=suite,
        office_id=office,
    )

    if gen_result.outcome.value != "success":
        print(f"  FAILED to create: {gen_result.error}")
        return

    doc_id = gen_result.data["document_id"]
    tokens_filled = gen_result.data.get("tokens_filled", "?")
    total_tokens = gen_result.data.get("total_tokens", "?")
    print(f"  Document created: {doc_id}")
    print(f"  Tokens filled: {tokens_filled} / {total_tokens}")

    # Wait for processing
    print("\n" + "=" * 70)
    print("  STEP 2: Wait for PandaDoc processing")
    print("=" * 70)

    for attempt in range(12):
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.pandadoc.com/public/v1/documents/{doc_id}",
                headers={"Authorization": f"API-Key {api_key}"},
                timeout=10,
            )
            status = resp.json().get("status", "")
            print(f"  [{attempt * 5}s] Status: {status}")
            if status == "document.draft":
                break
        await asyncio.sleep(5)
    else:
        print("  Timed out waiting for draft status")
        return

    # Send
    print("\n" + "=" * 70)
    print("  STEP 3: Send document for signature")
    print("=" * 70)

    send_result = await execute_pandadoc_contract_send(
        payload={
            "document_id": doc_id,
            "message": "Please review and sign this NDA.",
            "subject": "NDA for Signature — Skytech Tower LLC",
            "silent": False,
        },
        correlation_id=f"{corr}-send",
        suite_id=suite,
        office_id=office,
    )

    if send_result.outcome.value != "success":
        print(f"  FAILED to send: {send_result.error}")
        receipt = send_result.receipt_data
        if receipt:
            print(f"  Reason: {receipt.get('reason_code', 'unknown')}")
        return

    print(f"  Document SENT!")

    # Create signing session
    print("\n" + "=" * 70)
    print("  STEP 4: Create embedded signing session")
    print("=" * 70)

    await asyncio.sleep(2)

    session_result = await execute_pandadoc_create_signing_session(
        payload={
            "document_id": doc_id,
            "recipient": org_email,
        },
        correlation_id=f"{corr}-session",
        suite_id=suite,
        office_id=office,
    )

    if session_result.outcome.value != "success":
        print(f"  FAILED to create session: {session_result.error}")
        return

    session_id = session_result.data["session_id"]
    expires = session_result.data.get("expires_at", "unknown")
    base_url = os.getenv("ASPIRE_BASE_URL", "https://www.aspireos.app")

    signing_url = f"{base_url}/sign/{doc_id}?session={session_id}"
    pandadoc_url = f"https://app.pandadoc.com/s/{session_id}"

    print()
    print("+" * 70)
    print("+")
    print("+  E2E TEST: FULL SUCCESS")
    print("+")
    print(f"+  Document ID: {doc_id}")
    print(f"+  Session ID:  {session_id}")
    print(f"+  Expires:     {expires}")
    print("+")
    print("+  ASPIRE SIGNING URL:")
    print(f"+  {signing_url}")
    print("+")
    print("+  PANDADOC DIRECT SIGNING URL:")
    print(f"+  {pandadoc_url}")
    print("+")
    print("+  EMAIL: Check owner@aspireos.app inbox for signing notification")
    print("+")
    print("+" * 70)


if __name__ == "__main__":
    asyncio.run(main())
