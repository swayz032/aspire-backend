"""Tec Documents Skill Pack Tests — Coverage for document.generate, preview, share.

Categories:
  1. Generate document (3 tests) — success, receipt, invalid template
  2. Preview document (2 tests) — signed URL, receipt
  3. Share document (2 tests) — YELLOW tier, approval_required
  4. GREEN tier verification (2 tests) — no approval for generate/preview
  5. Tool executor integration (1 test) — puppeteer called + S3 upload

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Fail-closed on missing parameters
  - Law #4: GREEN for generate/preview, YELLOW for share
  - Law #6: Tenant isolation enforced on preview/share
"""

from __future__ import annotations

from typing import Any

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.tec_documents import (
    ACTOR_TEC,
    DEFAULT_BUCKET,
    VALID_TEMPLATE_TYPES,
    TecContext,
    TecDocumentsSkillPack,
)

# =============================================================================
# Fixtures
# =============================================================================

SUITE_A = "suite-tec-a-001"
SUITE_B = "suite-tec-b-002"
OFFICE = "office-tec-001"
CORR_ID = "corr-tec-test-001"


@pytest.fixture
def ctx_a() -> TecContext:
    """Context for Suite A."""
    return TecContext(suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID)


@pytest.fixture
def ctx_b() -> TecContext:
    """Context for Suite B."""
    return TecContext(suite_id=SUITE_B, office_id=OFFICE, correlation_id=CORR_ID)


@pytest.fixture
def skillpack() -> TecDocumentsSkillPack:
    """Fresh skill pack instance."""
    return TecDocumentsSkillPack()


def _mock_tool_success(**kwargs: Any) -> ToolExecutionResult:
    """Mock tool executor that always succeeds."""
    tool_id = kwargs.get("tool_id", "unknown")
    data: dict[str, Any] = {"stub": True}

    if tool_id == "puppeteer.pdf.generate":
        data["pdf_generated"] = True
        data["format"] = "A4"
    elif tool_id == "s3.document.upload":
        data["uploaded"] = True
        data["bucket"] = kwargs.get("payload", {}).get("bucket", "")
        data["key"] = kwargs.get("payload", {}).get("key", "")
        data["etag"] = "stub-etag-123"
    elif tool_id == "s3.url.sign":
        bucket = kwargs.get("payload", {}).get("bucket", DEFAULT_BUCKET)
        key = kwargs.get("payload", {}).get("key", "")
        data["presigned_url"] = f"https://{bucket}.s3.us-east-1.amazonaws.com/{key}?signed=true"
        data["bucket"] = bucket
        data["key"] = key
        data["expires_in"] = kwargs.get("payload", {}).get("expires_in", 3600)

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=data,
        receipt_data={"receipt_id": "mock-receipt-001"},
    )


async def mock_tool_success(**kwargs: Any) -> ToolExecutionResult:
    """Async mock tool executor that always succeeds."""
    return _mock_tool_success(**kwargs)


async def mock_tool_fail(**kwargs: Any) -> ToolExecutionResult:
    """Async mock tool executor that always fails."""
    tool_id = kwargs.get("tool_id", "unknown")
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error=f"Mock failure for {tool_id}",
        receipt_data={"receipt_id": "mock-fail-receipt"},
    )


# Track which tools were called
_tool_calls: list[str] = []


async def mock_tool_tracking(**kwargs: Any) -> ToolExecutionResult:
    """Async mock that tracks which tools are called."""
    tool_id = kwargs.get("tool_id", "unknown")
    _tool_calls.append(tool_id)
    return _mock_tool_success(**kwargs)


# =============================================================================
# 1. Generate Document Tests
# =============================================================================


@pytest.mark.asyncio
async def test_generate_document_success(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Generate a document from a valid template — should succeed."""
    result = await skillpack.generate_document(
        template_id="invoice",
        data={"title": "Test Invoice", "body": "<p>Line items here</p>"},
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is True
    assert result.error is None
    assert result.data["template_id"] == "invoice"
    assert result.data["bucket"] == DEFAULT_BUCKET
    assert result.data["key"].startswith(f"{SUITE_A}/invoice/")
    assert result.data["key"].endswith(".pdf")


@pytest.mark.asyncio
async def test_generate_document_receipt(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Generate document must emit a receipt (Law #2)."""
    result = await skillpack.generate_document(
        template_id="proposal",
        data={"title": "Proposal Draft"},
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is True
    receipt = result.receipt
    assert receipt["event_type"] == "document.generate"
    assert receipt["status"] == "ok"
    assert receipt["suite_id"] == SUITE_A
    assert receipt["office_id"] == OFFICE
    assert receipt["actor"] == ACTOR_TEC
    assert receipt["correlation_id"] == CORR_ID
    assert "receipt_id" in receipt
    assert "inputs_hash" in receipt
    assert receipt["inputs_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_generate_document_invalid_template(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Invalid template_id should fail closed (Law #3)."""
    result = await skillpack.generate_document(
        template_id="malicious_template",
        data={},
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is False
    assert "Invalid template_id" in (result.error or "")
    # Still emits a receipt even on failure (Law #2)
    assert result.receipt["status"] == "denied"
    assert result.receipt["event_type"] == "document.generate"


# =============================================================================
# 2. Preview Document Tests
# =============================================================================


@pytest.mark.asyncio
async def test_preview_document_signed_url(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Preview should return a presigned URL."""
    doc_id = f"{SUITE_A}/invoice/{CORR_ID}.pdf"

    result = await skillpack.preview_document(
        document_id=doc_id,
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is True
    assert "presigned_url" in result.data
    assert result.data["document_id"] == doc_id
    assert result.data["expires_in"] == 3600


@pytest.mark.asyncio
async def test_preview_document_receipt(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Preview must emit a receipt (Law #2)."""
    doc_id = f"{SUITE_A}/report/{CORR_ID}.pdf"

    result = await skillpack.preview_document(
        document_id=doc_id,
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is True
    receipt = result.receipt
    assert receipt["event_type"] == "document.preview"
    assert receipt["status"] == "ok"
    assert receipt["suite_id"] == SUITE_A
    assert receipt["actor"] == ACTOR_TEC


# =============================================================================
# 3. Share Document Tests
# =============================================================================


@pytest.mark.asyncio
async def test_share_document_yellow_tier(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Share document is YELLOW tier — risk_tier must be yellow."""
    doc_id = f"{SUITE_A}/contract/{CORR_ID}.pdf"

    result = await skillpack.share_document(
        document_id=doc_id,
        recipients=["client@example.com"],
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is True
    assert result.data["risk_tier"] == "yellow"
    assert result.receipt["risk_tier"] == "yellow"


@pytest.mark.asyncio
async def test_share_document_approval_required(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Share document must require approval (Law #4, YELLOW tier)."""
    doc_id = f"{SUITE_A}/proposal/{CORR_ID}.pdf"

    result = await skillpack.share_document(
        document_id=doc_id,
        recipients=["buyer@example.com", "partner@example.com"],
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is True
    assert result.data["approval_required"] is True
    assert result.data["recipients"] == ["buyer@example.com", "partner@example.com"]
    assert result.receipt["status"] == "approval_required"
    assert result.receipt["metadata"]["approval_required"] is True


# =============================================================================
# 4. GREEN Tier Verification
# =============================================================================


@pytest.mark.asyncio
async def test_green_tier_no_approval_for_generate(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Generate is GREEN — no approval_required in result."""
    result = await skillpack.generate_document(
        template_id="report",
        data={"title": "Q1 Report"},
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is True
    assert "approval_required" not in result.data
    assert result.receipt["risk_tier"] == "green"


@pytest.mark.asyncio
async def test_green_tier_no_approval_for_preview(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Preview is GREEN — no approval_required in result."""
    doc_id = f"{SUITE_A}/letter/{CORR_ID}.pdf"

    result = await skillpack.preview_document(
        document_id=doc_id,
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is True
    assert "approval_required" not in result.data
    assert result.receipt["risk_tier"] == "green"


# =============================================================================
# 5. Tool Executor Integration
# =============================================================================


@pytest.mark.asyncio
async def test_tool_executor_puppeteer_called(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Generate document must call puppeteer.pdf.generate then s3.document.upload."""
    _tool_calls.clear()

    result = await skillpack.generate_document(
        template_id="invoice",
        data={"title": "Tracked Invoice"},
        context=ctx_a,
        execute_tool_fn=mock_tool_tracking,
    )

    assert result.success is True
    assert "puppeteer.pdf.generate" in _tool_calls
    assert "s3.document.upload" in _tool_calls
    # Puppeteer must be called before S3 upload
    assert _tool_calls.index("puppeteer.pdf.generate") < _tool_calls.index("s3.document.upload")


@pytest.mark.asyncio
async def test_s3_upload_after_generate(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """If PDF generation fails, S3 upload must NOT be called (fail-closed)."""
    calls: list[str] = []

    async def mock_fail_on_puppeteer(**kwargs: Any) -> ToolExecutionResult:
        tool_id = kwargs.get("tool_id", "unknown")
        calls.append(tool_id)
        if tool_id == "puppeteer.pdf.generate":
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id=tool_id,
                error="Puppeteer crashed",
                receipt_data={"receipt_id": "fail-receipt"},
            )
        return _mock_tool_success(**kwargs)

    result = await skillpack.generate_document(
        template_id="invoice",
        data={"title": "Should Fail"},
        context=ctx_a,
        execute_tool_fn=mock_fail_on_puppeteer,
    )

    assert result.success is False
    assert "puppeteer.pdf.generate" in calls
    assert "s3.document.upload" not in calls  # Must NOT be called
    assert result.receipt["status"] == "failed"


# =============================================================================
# 6. Tenant Isolation (Law #6)
# =============================================================================


@pytest.mark.asyncio
async def test_preview_cross_tenant_denied(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Preview must deny access to documents from another tenant (Law #6)."""
    # Document belongs to SUITE_B, but ctx is SUITE_A
    doc_id = f"{SUITE_B}/invoice/some-doc.pdf"

    result = await skillpack.preview_document(
        document_id=doc_id,
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is False
    assert "tenant" in (result.error or "").lower()
    assert result.receipt["status"] == "denied"
    assert result.receipt["metadata"]["reason"] == "TENANT_ISOLATION_VIOLATION"


@pytest.mark.asyncio
async def test_share_cross_tenant_denied(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Share must deny access to documents from another tenant (Law #6)."""
    doc_id = f"{SUITE_B}/proposal/some-doc.pdf"

    result = await skillpack.share_document(
        document_id=doc_id,
        recipients=["external@example.com"],
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is False
    assert "tenant" in (result.error or "").lower()
    assert result.receipt["status"] == "denied"


# =============================================================================
# 7. Missing Parameter Validation (Law #3)
# =============================================================================


@pytest.mark.asyncio
async def test_generate_missing_template_id(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Missing template_id must fail closed (Law #3)."""
    result = await skillpack.generate_document(
        template_id="",
        data={},
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is False
    assert "template_id" in (result.error or "")
    assert result.receipt["status"] == "denied"


@pytest.mark.asyncio
async def test_preview_missing_document_id(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Missing document_id must fail closed (Law #3)."""
    result = await skillpack.preview_document(
        document_id="",
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is False
    assert "document_id" in (result.error or "")
    assert result.receipt["status"] == "denied"


@pytest.mark.asyncio
async def test_share_missing_recipients(
    skillpack: TecDocumentsSkillPack,
    ctx_a: TecContext,
) -> None:
    """Missing recipients must fail closed (Law #3)."""
    doc_id = f"{SUITE_A}/invoice/doc.pdf"

    result = await skillpack.share_document(
        document_id=doc_id,
        recipients=[],
        context=ctx_a,
        execute_tool_fn=mock_tool_success,
    )

    assert result.success is False
    assert "recipients" in (result.error or "")
    assert result.receipt["status"] == "denied"
