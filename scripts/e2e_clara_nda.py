"""E2E Clara NDA Test — Real pipeline through Ava → Clara → PandaDoc.

Usage:
    # 1. Start orchestrator:
    #    cd backend/orchestrator && source ~/venvs/aspire/bin/activate
    #    ASPIRE_TOKEN_SIGNING_KEY=test-key-32chars-minimum-length uvicorn aspire_orchestrator.server:app --port 8000
    #
    # 2. Run this script:
    #    python scripts/e2e_clara_nda.py
    #
    # 3. Check PandaDoc sandbox dashboard to verify document was created:
    #    https://app.pandadoc.com/a/#/documents

Flow:
    Step 1: POST /v1/intents → Ava classifies "Draft an NDA" → routes to Clara
    Step 2: Policy eval → YELLOW tier → draft-first → 202 APPROVAL_REQUIRED
    Step 3: Extract approval_id from response
    Step 4: POST /v1/resume/{approval_id} → Clara calls PandaDoc API
    Step 5: Verify PandaDoc document was actually created
    Step 6: Check receipts for full trace chain
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx

# Try to load .env for Supabase credentials
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", "orchestrator", ".env"))
except ImportError:
    pass

# --- Config ---
ORCHESTRATOR_URL = "http://localhost:8000"
PANDADOC_API_KEY = "e7d42f15fad040c428ddcf4962b793a4ca6a9247"  # Sandbox key
SUPABASE_URL = os.environ.get("ASPIRE_SUPABASE_URL", "https://qtuehjqlcmfcascqjjhc.supabase.co")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("ASPIRE_SUPABASE_SERVICE_ROLE_KEY", "")

# Real test tenant (exists in Supabase — founder.test@gmail.com)
SUITE_ID = "c4eebdbd-e019-42c0-9143-077762e92bbc"
OFFICE_ID = "c4eebdbd-e019-42c0-9143-077762e92bbc"  # same as suite for single-office tenant
ACTOR_ID = "69d09144-1efc-4c9c-9bde-44bf2e7e9488"  # test user UUID

HEADERS = {
    "Content-Type": "application/json",
    "x-suite-id": SUITE_ID,
    "x-office-id": OFFICE_ID,
    "x-actor-id": ACTOR_ID,
}


def log(step: str, msg: str, data: dict | None = None) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] === {step} ===")
    print(f"  {msg}")
    if data:
        print(f"  {json.dumps(data, indent=2, default=str)[:1000]}")


async def main() -> None:
    correlation_id = str(uuid.uuid4())

    async with httpx.AsyncClient(timeout=30.0) as client:

        # --- Step 0: Health check ---
        log("STEP 0", "Checking orchestrator health...")
        try:
            r = await client.get(f"{ORCHESTRATOR_URL}/healthz")
            if r.status_code != 200:
                log("FAIL", f"Orchestrator not healthy: {r.status_code}")
                sys.exit(1)
            log("STEP 0", "Orchestrator is healthy", r.json())
        except httpx.ConnectError:
            log("FAIL", "Cannot connect to orchestrator at localhost:8000")
            log("HINT", "Start it with: cd backend/orchestrator && uvicorn aspire_orchestrator.server:app --port 8000")
            sys.exit(1)

        # --- Step 1: Send NDA intent to Ava ---
        log("STEP 1", "Sending NDA draft intent to Ava...")
        intent_payload = {
            "schema_version": "1.0",
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "request_id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": "contract.generate",
            "utterance": "Draft a mutual NDA between Acme Corp and Wayne Enterprises for a potential business partnership",
            "payload": {
                "utterance": "Draft a mutual NDA between Acme Corp and Wayne Enterprises for a potential business partnership",
                "template_type": "general_mutual_nda",
                "parties": [
                    {"name": "Acme Corp", "email": "legal@acme-test.com", "role": "owner_signer"},
                    {"name": "Wayne Enterprises", "email": "bruce@wayne-test.com", "role": "client_signer"},
                ],
                "terms": {
                    "title": "Mutual NDA - Acme Corp / Wayne Enterprises",
                    "purpose": "Exploring potential business partnership",
                    "term_length": "2 years",
                    "jurisdiction_state": "NY",
                },
            },
        }

        r = await client.post(
            f"{ORCHESTRATOR_URL}/v1/intents",
            json=intent_payload,
            headers={**HEADERS, "x-correlation-id": correlation_id},
        )

        log("STEP 1", f"Response status: {r.status_code}")
        response_data = r.json()
        log("STEP 1", "Response body:", response_data)

        # --- Step 2: Check if we got APPROVAL_REQUIRED (draft-first) ---
        if r.status_code == 202:
            log("STEP 2", "Got 202 APPROVAL_REQUIRED (draft-first working!)")

            # Extract approval_id — could be in draft_id or approval_id field
            approval_id = (
                response_data.get("draft_id")
                or response_data.get("approval_id")
                or response_data.get("data", {}).get("approval_id")
                or response_data.get("data", {}).get("draft_id")
            )

            if not approval_id:
                log("FAIL", "Cannot find draft_id/approval_id in response — cannot resume")
                log("DEBUG", "Full response:", response_data)
                sys.exit(1)

            log("STEP 2", f"Approval ID (draft_id): {approval_id}")

            # --- Step 2.5: Approve the draft in Supabase (simulates Authority Queue "Approve" button) ---
            log("STEP 2.5", "Approving draft in Supabase (simulating Authority Queue)...")
            if not SUPABASE_SERVICE_ROLE_KEY:
                log("FAIL", "ASPIRE_SUPABASE_SERVICE_ROLE_KEY not set — cannot approve draft")
                sys.exit(1)

            supabase_headers = {
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }
            approve_url = f"{SUPABASE_URL}/rest/v1/approval_requests?approval_id=eq.{approval_id}"
            r_approve = await client.patch(
                approve_url,
                json={
                    "status": "approved",
                    "decided_at": datetime.now(timezone.utc).isoformat(),
                    "decided_by_user_id": ACTOR_ID,
                    "decision_surface": "e2e_test",
                },
                headers=supabase_headers,
            )
            if r_approve.status_code in (200, 204):
                log("STEP 2.5", "Draft approved in Supabase!", r_approve.json() if r_approve.text else None)
            else:
                log("FAIL", f"Approval failed: {r_approve.status_code}", r_approve.json() if r_approve.text else None)
                sys.exit(1)

            # --- Step 3: Resume execution ---
            log("STEP 3", f"Resuming execution via POST /v1/resume/{approval_id}...")

            r2 = await client.post(
                f"{ORCHESTRATOR_URL}/v1/resume/{approval_id}",
                headers=HEADERS,
            )

            log("STEP 3", f"Resume response status: {r2.status_code}")
            resume_data = r2.json()
            log("STEP 3", "Resume response:", resume_data)

            if r2.status_code == 200 and resume_data.get("narration"):
                log("SUCCESS", f"Narration: {resume_data['narration']}")

                # Check for PandaDoc document ID in the result
                exec_result = resume_data.get("data", {})
                doc_id = (
                    exec_result.get("document_id")
                    or exec_result.get("pandadoc_document_id")
                    or exec_result.get("id")
                )

                if doc_id:
                    log("STEP 4", f"PandaDoc document ID: {doc_id}")

                    # Verify document exists in PandaDoc
                    log("STEP 4", "Verifying document in PandaDoc sandbox...")
                    r3 = await client.get(
                        f"https://api.pandadoc.com/public/v1/documents/{doc_id}",
                        headers={"Authorization": f"API-Key {PANDADOC_API_KEY}"},
                    )
                    if r3.status_code == 200:
                        pd_doc = r3.json()
                        log("VERIFIED", "Document EXISTS in PandaDoc!", {
                            "id": pd_doc.get("id"),
                            "name": pd_doc.get("name"),
                            "status": pd_doc.get("status"),
                            "created_date": pd_doc.get("date_created"),
                        })
                    else:
                        log("WARNING", f"PandaDoc verification failed: {r3.status_code}", r3.json() if r3.status_code < 500 else {})
                else:
                    log("STEP 4", "No PandaDoc document ID in response — check if using stub executor")
                    log("DEBUG", "Execution result:", exec_result)
            else:
                log("FAIL", f"Resume failed: {resume_data.get('error', 'unknown')}")

        elif r.status_code == 200:
            log("STEP 2", "Got 200 — intent processed (might be GREEN tier or auto-approved)")
            log("RESULT", "Full response:", response_data)

        elif r.status_code == 403:
            error = response_data.get("error", "")
            log("STEP 2", f"Got 403 — denied: {error}")
            if error == "POLICY_DENIED":
                log("DEBUG", "Policy denied the action — check policy_matrix.yaml")
            elif error == "SAFETY_BLOCKED":
                log("DEBUG", "Safety gate blocked — check for jailbreak patterns")
            log("RESULT", "Full response:", response_data)

        else:
            log("STEP 2", f"Unexpected status: {r.status_code}")
            log("RESULT", "Full response:", response_data)

        # --- Step 5: Check receipts ---
        log("STEP 5", f"Querying receipts for correlation_id={correlation_id[:8]}...")
        r4 = await client.get(
            f"{ORCHESTRATOR_URL}/v1/receipts",
            params={"suite_id": SUITE_ID, "correlation_id": correlation_id},
            headers=HEADERS,
        )
        if r4.status_code == 200:
            receipts = r4.json()
            if isinstance(receipts, list):
                log("STEP 5", f"Found {len(receipts)} receipts in trace chain")
                for i, rcpt in enumerate(receipts):
                    print(f"    [{i+1}] {rcpt.get('action_type', '?')} | {rcpt.get('outcome', '?')} | {rcpt.get('reason_code', '')}")
            elif isinstance(receipts, dict) and "receipts" in receipts:
                receipt_list = receipts["receipts"]
                log("STEP 5", f"Found {len(receipt_list)} receipts in trace chain")
                for i, rcpt in enumerate(receipt_list):
                    print(f"    [{i+1}] {rcpt.get('action_type', '?')} | {rcpt.get('outcome', '?')} | {rcpt.get('reason_code', '')}")
            else:
                log("STEP 5", "Receipts response:", receipts)
        else:
            log("STEP 5", f"Receipt query returned {r4.status_code}")

    log("DONE", "E2E test complete! Check PandaDoc dashboard at https://app.pandadoc.com/a/#/documents")


if __name__ == "__main__":
    asyncio.run(main())
