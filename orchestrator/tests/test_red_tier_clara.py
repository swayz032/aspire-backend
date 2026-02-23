"""Clara Legal Skill Pack Tests -- RED tier contract management.

Tests cover:
  - 3 generate_contract: yellow_tier / template_types / receipt
  - 2 review_contract: green_tier / success
  - 3 sign_contract: red_tier / presence / binding_fields
  - 2 track_compliance: green_tier / expiration_detection
  - 5 evil tests: sign without presence, sign wrong contract, cross-tenant,
    forge signer, bypass approval

Law coverage:
  - Law #2: Every test verifies receipt generation
  - Law #3: Missing fields -> deny with receipt
  - Law #4: Risk tiers enforced (GREEN/YELLOW/RED)
  - Law #6: Cross-tenant isolation evil test
  - Law #8: Presence required for RED (contract.sign)
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.clara_legal import (
    ACTOR_CLARA,
    CONTRACT_GENERATE_BINDING_FIELDS,
    CONTRACT_SIGN_BINDING_FIELDS,
    VALID_TEMPLATE_TYPES,
    ClaraContext,
    ClaraLegalSkillPack,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ctx(
    suite_id: str = "suite-001",
    office_id: str = "office-001",
) -> ClaraContext:
    return ClaraContext(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=str(uuid.uuid4()),
        capability_token_id=f"cap-{uuid.uuid4().hex[:8]}",
        capability_token_hash=f"sha256:{uuid.uuid4().hex}",
    )


def _mock_tool_result(
    *,
    success: bool = True,
    data: dict | None = None,
    error: str | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS if success else Outcome.FAILED,
        tool_id="pandadoc.contract.read",
        data=data or {
            "document_id": "doc-abc123",
            "name": "Test NDA",
            "status": "document.completed",
            "date_created": "2026-02-14T10:00:00Z",
            "date_modified": "2026-02-14T12:00:00Z",
            "expiration_date": "2027-02-14T00:00:00Z",
        },
        error=error,
        receipt_data={"receipt_id": "test-receipt"},
    )


@pytest.fixture
def clara() -> ClaraLegalSkillPack:
    return ClaraLegalSkillPack()


# ===========================================================================
# generate_contract tests (3) -- YELLOW tier
# ===========================================================================


@pytest.mark.asyncio
async def test_generate_contract_yellow_tier_approval_required(clara: ClaraLegalSkillPack) -> None:
    """generate_contract should return approval_required=True (YELLOW tier)."""
    ctx = _ctx()
    result = await clara.generate_contract(
        template_type="nda",
        parties=[{"name": "Acme Corp", "email": "legal@acme.com", "role": "party_a"}],
        terms={
            "title": "Non-Disclosure Agreement",
            "duration": "2 years",
            "jurisdiction_state": "NY",
            "purpose": "Business partnership",
            "term_length": "2 years",
        },
        context=ctx,
    )

    assert result.success is True
    assert result.approval_required is True
    assert result.presence_required is False  # YELLOW, not RED
    assert result.data["risk_tier"] == "yellow"
    assert result.data["template_type"] == "general_mutual_nda"  # resolved from alias
    # Receipt emitted (Law #2)
    assert result.receipt["receipt_id"].startswith("rcpt-clara-")
    assert result.receipt["event_type"] == "contract.generate"
    assert result.receipt["actor"] == ACTOR_CLARA
    assert result.receipt["suite_id"] == ctx.suite_id
    assert result.receipt["status"] == "ok"


@pytest.mark.asyncio
async def test_generate_contract_all_template_types(clara: ClaraLegalSkillPack) -> None:
    """generate_contract should accept all valid template types.

    Each template has its own jurisdiction and preflight requirements.
    We provide a superset of all possible required fields so every
    template passes validation.
    """
    from aspire_orchestrator.skillpacks.clara_legal import (
        _resolve_template_key,
        get_template_spec,
    )

    ctx = _ctx()
    # Superset of all required_fields_delta across all 14 real templates
    all_terms: dict[str, Any] = {
        "title": "Test",
        "jurisdiction_state": "NY",
        "purpose": "Testing",
        "term_length": "1 year",
        "scope_description": "Test scope",
        "milestones": "M1",
        "pricing": "$100",
        "schedule": "ASAP",
        "budget": "$50,000",
        "project_timeline": "6 months",
        "contract_value": "$75,000",
        "services_scope": "Bookkeeping",
        "fee_schedule": "$200/mo",
        "tax_year": "2025",
        "filing_type": "1040",
        "taxpayer_name": "Test Taxpayer",
        "business_type": "LLC",
        "property_address": "123 Main St",
        "lease_term": "12 months",
        "monthly_rent": "$1500",
        "disclosing_party": "Acme Corp",
    }

    for ttype in VALID_TEMPLATE_TYPES:
        resolved = _resolve_template_key(ttype)
        spec = get_template_spec(ttype)
        if spec is None:
            # Legacy alias that resolves to None (e.g., "termination")
            continue

        result = await clara.generate_contract(
            template_type=ttype,
            parties=[{"name": "Test Party", "email": "test@example.com", "role": "signer"}],
            terms=all_terms,
            context=ctx,
        )
        assert result.success is True, (
            f"Template type '{ttype}' (resolved: {resolved}) should succeed, "
            f"got error: {result.error}"
        )
        assert result.data["template_type"] == resolved


@pytest.mark.asyncio
async def test_generate_contract_receipt_has_required_fields(clara: ClaraLegalSkillPack) -> None:
    """generate_contract receipt must have all Law #2 required fields."""
    ctx = _ctx()
    result = await clara.generate_contract(
        template_type="sow",
        parties=[
            {"name": "Client LLC", "email": "client@example.com", "role": "client"},
            {"name": "Vendor Inc", "email": "vendor@example.com", "role": "vendor"},
        ],
        terms={
            "title": "Scope of Work",
            "jurisdiction_state": "CA",
            "milestones": "Phase 1: Design, Phase 2: Build",
            "pricing": "$50,000",
        },
        context=ctx,
    )

    receipt = result.receipt
    assert "receipt_id" in receipt
    assert "ts" in receipt
    assert "event_type" in receipt
    assert "suite_id" in receipt
    assert "office_id" in receipt
    assert "actor" in receipt
    assert "correlation_id" in receipt
    assert "status" in receipt
    assert "inputs_hash" in receipt
    assert "policy" in receipt
    assert receipt["policy"]["decision"] == "allow"
    # Metadata includes party info — resolved from "sow" alias
    assert receipt["metadata"]["template_type"] == "trades_sow"
    assert receipt["metadata"]["party_count"] == 2
    assert "Client LLC" in receipt["metadata"]["party_names"]


# ===========================================================================
# review_contract tests (2) -- GREEN tier
# ===========================================================================


@pytest.mark.asyncio
async def test_review_contract_green_tier_success(clara: ClaraLegalSkillPack) -> None:
    """review_contract should execute directly (GREEN, no approval) and return contract data."""
    ctx = _ctx()
    mock_result = _mock_tool_result(success=True)

    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_exec:
        result = await clara.review_contract(
            contract_id="doc-abc123",
            context=ctx,
        )

    assert result.success is True
    assert result.approval_required is False
    assert result.presence_required is False
    assert result.data["document_id"] == "doc-abc123"
    # Verify tool was called with correct params
    mock_exec.assert_called_once()
    call_kwargs = mock_exec.call_args.kwargs
    assert call_kwargs["tool_id"] == "pandadoc.contract.read"
    assert call_kwargs["risk_tier"] == "green"
    assert call_kwargs["suite_id"] == ctx.suite_id
    # Receipt emitted
    assert result.receipt["event_type"] == "contract.review"
    assert result.receipt["status"] == "ok"


@pytest.mark.asyncio
async def test_review_contract_missing_contract_id(clara: ClaraLegalSkillPack) -> None:
    """review_contract should fail closed with receipt when contract_id is missing."""
    ctx = _ctx()
    result = await clara.review_contract(contract_id="", context=ctx)

    assert result.success is False
    assert "contract_id" in (result.error or "")
    # Receipt emitted on denial (Law #2 + Law #3)
    assert result.receipt["status"] == "denied"
    assert result.receipt["policy"]["decision"] == "deny"
    assert "MISSING_CONTRACT_ID" in result.receipt["policy"]["reasons"]


# ===========================================================================
# sign_contract tests (3) -- RED tier
# ===========================================================================


@pytest.mark.asyncio
async def test_sign_contract_red_tier_presence_required(clara: ClaraLegalSkillPack) -> None:
    """sign_contract must require both approval and presence (RED tier)."""
    ctx = _ctx()
    result = await clara.sign_contract(
        contract_id="doc-abc123",
        signer_info={
            "signer_name": "Jane Doe",
            "signer_email": "jane@example.com",
        },
        context=ctx,
    )

    assert result.success is True
    assert result.approval_required is True
    assert result.presence_required is True  # RED tier demands presence
    assert result.data["risk_tier"] == "red"
    assert result.data["presence_required"] is True
    assert result.data["contract_id"] == "doc-abc123"
    assert result.data["signer_name"] == "Jane Doe"
    assert result.data["signer_email"] == "jane@example.com"


@pytest.mark.asyncio
async def test_sign_contract_binding_fields_in_receipt(clara: ClaraLegalSkillPack) -> None:
    """sign_contract receipt must include all binding fields for audit trail."""
    ctx = _ctx()
    result = await clara.sign_contract(
        contract_id="doc-xyz789",
        signer_info={
            "signer_name": "John Smith",
            "signer_email": "john@company.com",
        },
        context=ctx,
    )

    receipt = result.receipt
    assert receipt["event_type"] == "contract.sign"
    assert receipt["actor"] == ACTOR_CLARA
    # Binding fields in metadata for audit trail
    assert receipt["metadata"]["contract_id"] == "doc-xyz789"
    # PII-masked in receipt (Law #9)
    assert receipt["metadata"]["signer_name"] == "J. S***"
    assert receipt["metadata"]["signer_email"] == "j***@company.com"
    assert "signature_timestamp" in receipt["metadata"]


@pytest.mark.asyncio
async def test_sign_contract_missing_binding_fields(clara: ClaraLegalSkillPack) -> None:
    """sign_contract must deny when binding fields are missing (Law #3)."""
    ctx = _ctx()

    # Missing signer_name and signer_email
    result = await clara.sign_contract(
        contract_id="doc-abc123",
        signer_info={},
        context=ctx,
    )

    assert result.success is False
    assert result.receipt["status"] == "denied"
    assert "MISSING_BINDING_FIELDS" in result.receipt["policy"]["reasons"]
    assert "signer_email" in (result.error or "")
    assert "signer_name" in (result.error or "")


# ===========================================================================
# track_compliance tests (2) -- GREEN tier
# ===========================================================================


@pytest.mark.asyncio
async def test_track_compliance_green_tier_success(clara: ClaraLegalSkillPack) -> None:
    """track_compliance should return compliance assessment (GREEN, no approval)."""
    ctx = _ctx()
    mock_result = _mock_tool_result(success=True)

    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await clara.track_compliance(
            contract_id="doc-abc123",
            context=ctx,
        )

    assert result.success is True
    assert result.approval_required is False
    assert result.presence_required is False
    assert result.data["contract_id"] == "doc-abc123"
    assert result.data["compliance_status"] == "active"
    assert result.data["expiration_date"] == "2027-02-14T00:00:00Z"
    # Receipt
    assert result.receipt["event_type"] == "contract.compliance"
    assert result.receipt["status"] == "ok"


@pytest.mark.asyncio
async def test_track_compliance_expiration_detection(clara: ClaraLegalSkillPack) -> None:
    """track_compliance should detect contract expiration status."""
    ctx = _ctx()
    mock_result = _mock_tool_result(
        success=True,
        data={
            "document_id": "doc-expired",
            "name": "Expired SoW",
            "status": "voided",
            "date_created": "2025-01-01T00:00:00Z",
            "date_modified": "2025-06-01T00:00:00Z",
            "expiration_date": "2025-12-31T00:00:00Z",
        },
    )

    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await clara.track_compliance(
            contract_id="doc-expired",
            context=ctx,
        )

    assert result.success is True
    assert result.data["compliance_status"] == "terminated"
    assert "Contract voided" in result.data["alerts"]


# ===========================================================================
# Evil tests (5) -- security-critical attack scenarios
# ===========================================================================


@pytest.mark.asyncio
async def test_evil_sign_without_presence_flag(clara: ClaraLegalSkillPack) -> None:
    """EVIL: sign_contract MUST always set presence_required=True for RED tier.

    Attacker scenario: Try to sign a contract hoping the system allows
    execution without video presence verification.
    """
    ctx = _ctx()
    result = await clara.sign_contract(
        contract_id="doc-evil-001",
        signer_info={
            "signer_name": "Evil Actor",
            "signer_email": "evil@malicious.com",
        },
        context=ctx,
    )

    # Even when all fields are provided, presence MUST be required
    assert result.presence_required is True, \
        "RED tier contract.sign MUST require presence verification"
    assert result.approval_required is True, \
        "RED tier contract.sign MUST require approval"
    assert result.data.get("presence_required") is True, \
        "Plan data MUST flag presence_required for downstream enforcement"


@pytest.mark.asyncio
async def test_evil_sign_wrong_contract_id(clara: ClaraLegalSkillPack) -> None:
    """EVIL: sign_contract must bind to exact contract_id in receipt.

    Attacker scenario: Approve signing contract A, then swap contract_id
    to contract B before execution (approve-then-swap attack).
    The receipt must capture the exact contract_id for verification.
    """
    ctx = _ctx()
    result_a = await clara.sign_contract(
        contract_id="doc-contract-A",
        signer_info={
            "signer_name": "Legit User",
            "signer_email": "legit@company.com",
        },
        context=ctx,
    )

    result_b = await clara.sign_contract(
        contract_id="doc-contract-B",
        signer_info={
            "signer_name": "Legit User",
            "signer_email": "legit@company.com",
        },
        context=ctx,
    )

    # Receipts must bind to different contract_ids
    assert result_a.receipt["metadata"]["contract_id"] == "doc-contract-A"
    assert result_b.receipt["metadata"]["contract_id"] == "doc-contract-B"
    # Input hashes must differ (different payloads)
    assert result_a.receipt["inputs_hash"] != result_b.receipt["inputs_hash"], \
        "Different contract_ids MUST produce different inputs_hash (approve-then-swap defense)"


@pytest.mark.asyncio
async def test_evil_cross_tenant_contract(clara: ClaraLegalSkillPack) -> None:
    """EVIL: Contract operations must be scoped to the caller's tenant (Law #6).

    Attacker scenario: Tenant B tries to review/sign Tenant A's contract.
    The skill pack must scope all receipts to the calling context's suite_id.
    """
    ctx_tenant_a = _ctx(suite_id="suite-tenant-A", office_id="office-A")
    ctx_tenant_b = _ctx(suite_id="suite-tenant-B", office_id="office-B")

    mock_result = _mock_tool_result(success=True)

    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_exec:
        result_a = await clara.review_contract("doc-shared", ctx_tenant_a)
        result_b = await clara.review_contract("doc-shared", ctx_tenant_b)

    # Verify tool_executor is called with the correct suite_id each time
    calls = mock_exec.call_args_list
    assert calls[0].kwargs["suite_id"] == "suite-tenant-A"
    assert calls[1].kwargs["suite_id"] == "suite-tenant-B"

    # Receipts must be scoped to their respective tenants
    assert result_a.receipt["suite_id"] == "suite-tenant-A"
    assert result_b.receipt["suite_id"] == "suite-tenant-B"


@pytest.mark.asyncio
async def test_evil_forge_signer_identity(clara: ClaraLegalSkillPack) -> None:
    """EVIL: sign_contract must capture signer identity in receipt for audit.

    Attacker scenario: User provides fake signer_name/email to forge
    someone else's signature. The receipt must capture exact inputs
    so fraud is detectable in the audit trail.
    """
    ctx = _ctx()
    result = await clara.sign_contract(
        contract_id="doc-forge-test",
        signer_info={
            "signer_name": "CEO <script>alert(1)</script>",
            "signer_email": "ceo@victim.com",
        },
        context=ctx,
    )

    # Receipt has PII-masked signer info (Law #9) — raw XSS payload is masked
    assert result.receipt["metadata"]["signer_name"] == "C. <***"
    assert result.receipt["metadata"]["signer_email"] == "c***@victim.com"
    # The inputs_hash binds these exact values
    assert result.receipt["inputs_hash"].startswith("sha256:")
    # Approval + presence still required
    assert result.approval_required is True
    assert result.presence_required is True


@pytest.mark.asyncio
async def test_evil_bypass_approval_invalid_template(clara: ClaraLegalSkillPack) -> None:
    """EVIL: generate_contract must reject invalid template types (Law #3).

    Attacker scenario: Pass a malicious template_type to bypass
    template validation and generate arbitrary documents.
    """
    ctx = _ctx()

    # Try various attack payloads
    # NOTE: "NDA" is a VALID case-insensitive alias (resolves to general_mutual_nda).
    # Only genuinely invalid templates should be tested here.
    evil_templates = [
        "",  # empty
        "../../etc/passwd",  # path traversal
        "admin_override",  # non-existent type
        "nda; DROP TABLE contracts",  # SQL injection
        "__proto__",  # prototype pollution attempt
    ]

    for evil_tpl in evil_templates:
        result = await clara.generate_contract(
            template_type=evil_tpl,
            parties=[{"name": "Attacker", "email": "attacker@evil.com"}],
            terms={"title": "Evil Contract"},
            context=ctx,
        )

        assert result.success is False, \
            f"Template '{evil_tpl}' MUST be rejected (fail closed)"
        assert result.receipt["status"] == "denied"
        assert "INVALID_TEMPLATE_TYPE" in result.receipt["policy"]["reasons"]


# ---------------------------------------------------------------------------
# Template Discovery Tests (browse_templates + get_template_details)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_templates_success(clara: ClaraLegalSkillPack) -> None:
    """browse_templates returns template list on success (GREEN, no approval)."""
    ctx = _ctx()
    mock_result = ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="pandadoc.templates.list",
        data={
            "templates": [
                {"id": "tmpl-abc", "name": "Mutual NDA"},
                {"id": "tmpl-def", "name": "Service Agreement"},
            ],
            "count": 2,
        },
    )

    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new=AsyncMock(return_value=mock_result),
    ) as mock_exec:
        result = await clara.browse_templates(query="NDA", context=ctx)

    assert result.success is True
    assert result.data["count"] == 2
    assert len(result.data["templates"]) == 2
    assert result.receipt["event_type"] == "templates.list"
    assert result.receipt["status"] == "ok"
    # GREEN tier: no approval_required
    assert result.approval_required is False
    # Verify the correct tool was called
    mock_exec.assert_called_once()
    call_kwargs = mock_exec.call_args[1]
    assert call_kwargs["tool_id"] == "pandadoc.templates.list"
    assert call_kwargs["payload"]["q"] == "NDA"


@pytest.mark.asyncio
async def test_browse_templates_no_query(clara: ClaraLegalSkillPack) -> None:
    """browse_templates works without a search query (lists all)."""
    ctx = _ctx()
    mock_result = ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="pandadoc.templates.list",
        data={"templates": [], "count": 0},
    )

    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new=AsyncMock(return_value=mock_result),
    ):
        result = await clara.browse_templates(query=None, context=ctx)

    assert result.success is True
    assert result.receipt["event_type"] == "templates.list"


@pytest.mark.asyncio
async def test_browse_templates_api_failure(clara: ClaraLegalSkillPack) -> None:
    """browse_templates returns failure receipt when PandaDoc is down."""
    ctx = _ctx()
    mock_result = ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id="pandadoc.templates.list",
        error="PandaDoc API error: HTTP 500",
    )

    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new=AsyncMock(return_value=mock_result),
    ):
        result = await clara.browse_templates(query="NDA", context=ctx)

    assert result.success is False
    assert result.receipt["status"] == "failed"
    assert result.error is not None


@pytest.mark.asyncio
async def test_get_template_details_success(clara: ClaraLegalSkillPack) -> None:
    """get_template_details returns fields/tokens/roles for a template."""
    ctx = _ctx()
    mock_result = ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="pandadoc.templates.details",
        data={
            "template_id": "tmpl-abc",
            "name": "Mutual NDA",
            "fields": [
                {"name": "Effective Date", "type": "date", "merge_field": ""},
            ],
            "tokens": [
                {"name": "Client.FirstName", "value": ""},
                {"name": "Client.LastName", "value": ""},
                {"name": "Company.Name", "value": ""},
            ],
            "roles": [
                {"name": "Owner", "signing_order": 1},
                {"name": "Counterparty", "signing_order": 2},
            ],
            "field_count": 1,
            "token_count": 3,
            "role_count": 2,
        },
    )

    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new=AsyncMock(return_value=mock_result),
    ) as mock_exec:
        result = await clara.get_template_details(
            template_id="tmpl-abc", context=ctx,
        )

    assert result.success is True
    assert result.data["token_count"] == 3
    assert result.data["role_count"] == 2
    assert result.receipt["event_type"] == "templates.details"
    assert result.receipt["status"] == "ok"
    # Receipt metadata should have counts
    assert result.receipt["metadata"]["field_count"] == 1
    assert result.receipt["metadata"]["token_count"] == 3
    # Verify correct tool call
    call_kwargs = mock_exec.call_args[1]
    assert call_kwargs["tool_id"] == "pandadoc.templates.details"
    assert call_kwargs["payload"]["template_id"] == "tmpl-abc"


@pytest.mark.asyncio
async def test_get_template_details_missing_id(clara: ClaraLegalSkillPack) -> None:
    """get_template_details denies with receipt when template_id is missing (Law #3)."""
    ctx = _ctx()
    result = await clara.get_template_details(template_id="", context=ctx)

    assert result.success is False
    assert result.receipt["status"] == "denied"
    assert "MISSING_TEMPLATE_ID" in result.receipt["policy"]["reasons"]


@pytest.mark.asyncio
async def test_get_template_details_whitespace_id(clara: ClaraLegalSkillPack) -> None:
    """get_template_details denies whitespace-only template_id (fail closed)."""
    ctx = _ctx()
    result = await clara.get_template_details(template_id="   ", context=ctx)

    assert result.success is False
    assert result.receipt["status"] == "denied"
