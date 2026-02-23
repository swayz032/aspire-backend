"""PandaDoc Sandbox E2E Tests — Real API calls against PandaDoc sandbox.

These tests are SKIPPED in CI unless ASPIRE_PANDADOC_API_KEY is set.
They verify the full Clara Legal → PandaDoc API integration:
  1. Generate a document from the sandbox
  2. Read document status
  3. Verify receipt chain

Run manually:
  ASPIRE_PANDADOC_API_KEY=<key> python -m pytest tests/test_pandadoc_e2e.py -v -s

Law coverage:
  - Law #2: Every API call produces a receipt
  - Law #3: Missing API key → fail closed
  - Law #6: Suite metadata embedded in PandaDoc document
  - Law #7: PandaDoc client is hands (execute only)
"""

from __future__ import annotations

import os
import pytest
import httpx

# Skip unless EXPLICITLY opted-in.  server.py's load_dotenv() runs at import
# time in other test modules, which puts ASPIRE_PANDADOC_API_KEY into os.environ
# even in normal test runs.  Require an explicit flag so these sandbox E2E tests
# only run when a developer intentionally invokes them.
PANDADOC_API_KEY = os.environ.get("ASPIRE_PANDADOC_API_KEY", "")
PANDADOC_E2E_ENABLED = os.environ.get("PANDADOC_E2E", "").lower() == "true"
pytestmark = pytest.mark.skipif(
    not PANDADOC_API_KEY or not PANDADOC_E2E_ENABLED,
    reason="PANDADOC_E2E=true not set — skipping PandaDoc sandbox E2E (run manually with PANDADOC_E2E=true)",
)


PANDADOC_BASE_URL = "https://api.pandadoc.com/public/v1"
HEADERS = {
    "Authorization": f"API-Key {PANDADOC_API_KEY}",
    "Content-Type": "application/json",
}


class TestPandaDocSandboxConnectivity:
    """Basic connectivity tests against PandaDoc sandbox API."""

    def test_api_key_valid(self) -> None:
        """Verify the sandbox API key is accepted by PandaDoc."""
        resp = httpx.get(
            f"{PANDADOC_BASE_URL}/documents",
            headers=HEADERS,
            params={"count": 1},
            timeout=15.0,
        )
        # 200 = valid key, even if empty result set
        assert resp.status_code == 200, f"PandaDoc API key invalid: {resp.status_code} {resp.text[:200]}"

    def test_list_templates(self) -> None:
        """List available templates in sandbox account."""
        resp = httpx.get(
            f"{PANDADOC_BASE_URL}/templates",
            headers=HEADERS,
            params={"count": 10},
            timeout=15.0,
        )
        assert resp.status_code == 200, f"Failed to list templates: {resp.status_code}"
        data = resp.json()
        # data.results contains template list
        results = data.get("results", [])
        print(f"\n  Sandbox templates: {len(results)}")
        for t in results[:5]:
            print(f"    - {t.get('name', '?')} (id={t.get('id', '?')})")


class TestPandaDocDocumentLifecycle:
    """Test document generation and reading via direct API."""

    def test_create_document_from_pdf_url(self) -> None:
        """Create a simple document in sandbox (no template required).

        Uses PandaDoc's document creation with inline content.
        This doesn't need a pre-existing template — perfect for sandbox testing.
        """
        body = {
            "name": "Aspire E2E Test NDA — Acme Corp / Wayne Enterprises",
            # PandaDoc requires url, template_uuid, or file.  Use a publicly
            # accessible sample PDF for sandbox testing.
            "url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
            "recipients": [
                {
                    "email": "legal@acme-test.com",
                    "first_name": "Legal",
                    "last_name": "Acme",
                    "role": "signer",
                },
                {
                    "email": "bruce@wayne-test.com",
                    "first_name": "Bruce",
                    "last_name": "Wayne",
                    "role": "signer",
                },
            ],
            "metadata": {
                "aspire_suite_id": "e2e-test-suite-001",
                "aspire_office_id": "e2e-test-office-001",
                "aspire_correlation_id": "e2e-corr-001",
                "aspire_template_key": "general_mutual_nda",
                "aspire_test": "true",
            },
            "tags": ["aspire-e2e-test"],
            "parse_form_fields": False,
        }

        resp = httpx.post(
            f"{PANDADOC_BASE_URL}/documents",
            headers=HEADERS,
            json=body,
            timeout=15.0,
        )

        # PandaDoc may return 200 or 201 for document creation
        assert resp.status_code in (200, 201), (
            f"Document creation failed: {resp.status_code} {resp.text[:300]}"
        )

        doc = resp.json()
        doc_id = doc.get("id", doc.get("uuid", ""))
        assert doc_id, f"No document ID in response: {doc}"

        print(f"\n  Document created: id={doc_id}")
        print(f"  Status: {doc.get('status', '?')}")
        print(f"  Name: {doc.get('name', '?')}")

        # Verify we can read it back
        read_resp = httpx.get(
            f"{PANDADOC_BASE_URL}/documents/{doc_id}",
            headers=HEADERS,
            timeout=15.0,
        )
        assert read_resp.status_code == 200, (
            f"Document read failed: {read_resp.status_code} {read_resp.text[:200]}"
        )

        read_doc = read_resp.json()
        assert read_doc.get("id") == doc_id or read_doc.get("uuid") == doc_id

        # Verify Aspire metadata was stored
        stored_metadata = read_doc.get("metadata", {})
        assert stored_metadata.get("aspire_suite_id") == "e2e-test-suite-001", (
            f"Suite ID not in metadata: {stored_metadata}"
        )

        print(f"  Read back: status={read_doc.get('status', '?')}")
        print(f"  Metadata: {stored_metadata}")


class TestPandaDocProviderClient:
    """Test the Aspire PandaDoc provider client against sandbox."""

    @pytest.mark.asyncio
    async def test_generate_via_client(self) -> None:
        """Test execute_pandadoc_contract_generate against real sandbox."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_contract_generate,
        )
        from aspire_orchestrator.models import Outcome

        result = await execute_pandadoc_contract_generate(
            payload={
                "template_id": "placeholder-template-id",  # Will fail gracefully
                "name": "Aspire Client Test — NDA",
                "recipients": [
                    {
                        "email": "test@aspire-e2e.com",
                        "first_name": "Test",
                        "last_name": "User",
                        "role": "signer",
                    },
                ],
                "metadata": {
                    "aspire_test": "true",
                },
            },
            correlation_id="e2e-client-test-001",
            suite_id="e2e-test-suite-001",
            office_id="e2e-test-office-001",
        )

        # With placeholder template_id, PandaDoc will return error
        # The key test is that the client handles it gracefully
        assert result.tool_id == "pandadoc.contract.generate"
        assert result.receipt_data is not None
        assert result.receipt_data.get("suite_id") == "e2e-test-suite-001"
        print(f"\n  Client result: outcome={result.outcome.value}")
        print(f"  Receipt: {result.receipt_data.get('reason_code', '?')}")

    @pytest.mark.asyncio
    async def test_read_via_client_nonexistent(self) -> None:
        """Test execute_pandadoc_contract_read with nonexistent doc ID."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_contract_read,
        )
        from aspire_orchestrator.models import Outcome

        result = await execute_pandadoc_contract_read(
            payload={"document_id": "nonexistent-doc-id-12345"},
            correlation_id="e2e-read-test-001",
            suite_id="e2e-test-suite-001",
            office_id="e2e-test-office-001",
        )

        # Should fail gracefully with DOMAIN_NOT_FOUND
        assert result.outcome == Outcome.FAILED
        assert result.tool_id == "pandadoc.contract.read"
        assert result.receipt_data is not None
        print(f"\n  Read nonexistent: outcome={result.outcome.value}")
        print(f"  Error: {result.error}")
