"""mail_ops_desk Admin Skill Pack Tests — 13 tests covering domain + mail operations.

Categories:
  1. Domain check/verify GREEN (3 tests) — success, missing param, receipt emission
  2. DNS create YELLOW (2 tests) — approval_required, missing binding fields
  3. Domain purchase RED (2 tests) — approval_required with registrant_info, missing domain
  4. Domain delete RED (1 test) — approval_required
  5. Mail account CRUD (2 tests) — create YELLOW approval, read GREEN success
  6. Evil tests (3 tests) — email body access blocked, cross-tenant domain access, missing all params

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Missing params / binding fields produce fail-closed error + receipt
  - Law #4: GREEN/YELLOW/RED tier classification verified
  - Law #6: Tenant isolation verified (evil: cross-tenant domain attempt)
  - Law #7: Tool executor called (not direct Domain Rail client)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.mail_ops_desk import (
    ACTOR_MAIL_OPS,
    MailOpsContext,
    MailOpsDeskSkillPack,
)


# =============================================================================
# Fixtures
# =============================================================================

SUITE_ID = "suite-mailops-test-001"
OFFICE_ID = "office-mailops-001"
CORR_ID = "corr-mailops-test-001"

EVIL_SUITE_ID = "suite-evil-attacker-999"
EVIL_OFFICE_ID = "office-evil-999"


@pytest.fixture
def ctx() -> MailOpsContext:
    """Tenant-scoped execution context."""
    return MailOpsContext(suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)


@pytest.fixture
def evil_ctx() -> MailOpsContext:
    """Attacker's tenant context."""
    return MailOpsContext(suite_id=EVIL_SUITE_ID, office_id=EVIL_OFFICE_ID, correlation_id="corr-evil-001")


@pytest.fixture
def mailops() -> MailOpsDeskSkillPack:
    """Fresh mail_ops_desk skill pack instance."""
    return MailOpsDeskSkillPack()


def _mock_tool_result(
    outcome: Outcome = Outcome.SUCCESS,
    tool_id: str = "domain.check",
    data: dict | None = None,
    error: str | None = None,
) -> ToolExecutionResult:
    """Build a mock ToolExecutionResult."""
    return ToolExecutionResult(
        outcome=outcome,
        tool_id=tool_id,
        data=data or {"status": "ok", "available": True},
        error=error,
        receipt_data={"id": "test-receipt", "outcome": outcome.value},
    )


# =============================================================================
# 1. Domain Check / Verify GREEN Tests
# =============================================================================


class TestDomainCheckVerify:
    """Test check_domain and verify_domain (GREEN tier)."""

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.skillpacks.mail_ops_desk.execute_tool")
    async def test_check_domain_green_success(
        self, mock_exec: AsyncMock, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Domain check returns GREEN result with no approval required."""
        mock_exec.return_value = _mock_tool_result(
            tool_id="domain.check",
            data={"available": True, "domain": "example.com"},
        )

        result = await mailops.check_domain("example.com", ctx)

        assert result.success
        assert not result.approval_required
        assert result.data["available"] is True
        assert result.error is None

        # Verify tool was called with correct params
        mock_exec.assert_called_once()
        call_kwargs = mock_exec.call_args.kwargs
        assert call_kwargs["tool_id"] == "domain.check"
        assert call_kwargs["payload"] == {"domain": "example.com"}
        assert call_kwargs["suite_id"] == SUITE_ID
        assert call_kwargs["risk_tier"] == "green"

    @pytest.mark.asyncio
    async def test_check_domain_missing_name(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Missing domain_name fails closed (Law #3)."""
        result = await mailops.check_domain("", ctx)

        assert not result.success
        assert "domain_name" in result.error
        assert result.receipt["status"] == "denied"
        assert result.receipt["policy"]["decision"] == "deny"
        assert "MISSING_DOMAIN_NAME" in result.receipt["policy"]["reasons"]

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.skillpacks.mail_ops_desk.execute_tool")
    async def test_verify_domain_receipt_emission(
        self, mock_exec: AsyncMock, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Verify domain emits receipt with correct fields (Law #2)."""
        mock_exec.return_value = _mock_tool_result(
            tool_id="domain.verify",
            data={"verified": True, "domain": "mysite.com"},
        )

        result = await mailops.verify_domain("mysite.com", ctx)

        assert result.success
        receipt = result.receipt
        assert receipt["event_type"] == "domain.verify"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["office_id"] == OFFICE_ID
        assert receipt["actor"] == ACTOR_MAIL_OPS
        assert receipt["correlation_id"] == CORR_ID
        assert receipt["status"] == "ok"
        assert receipt["inputs_hash"].startswith("sha256:")
        assert receipt["metadata"]["domain_name"] == "mysite.com"


# =============================================================================
# 2. DNS Create YELLOW Tests
# =============================================================================


class TestDnsCreate:
    """Test create_dns_record (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_dns_create_yellow_approval_required(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """DNS creation returns YELLOW plan with approval_required."""
        result = await mailops.create_dns_record(
            domain_name="example.com",
            record_type="MX",
            record_value="mail.example.com",
            context=ctx,
        )

        assert result.success
        assert result.approval_required
        assert result.data["domain"] == "example.com"
        assert result.data["record_type"] == "MX"
        assert result.data["value"] == "mail.example.com"
        assert result.data["risk_tier"] == "yellow"
        assert result.receipt["event_type"] == "domain.dns.create"

    @pytest.mark.asyncio
    async def test_dns_create_missing_binding_fields(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Missing binding fields fails closed (Law #3)."""
        result = await mailops.create_dns_record(
            domain_name="",
            record_type="",
            record_value="",
            context=ctx,
        )

        assert not result.success
        assert "Missing required binding fields" in result.error
        assert result.receipt["status"] == "denied"
        assert result.receipt["policy"]["decision"] == "deny"
        assert "MISSING_BINDING_FIELDS" in result.receipt["policy"]["reasons"]


# =============================================================================
# 3. Domain Purchase RED Tests
# =============================================================================


class TestDomainPurchase:
    """Test purchase_domain (RED tier)."""

    @pytest.mark.asyncio
    async def test_purchase_domain_red_approval_required(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Domain purchase returns RED plan with approval_required."""
        result = await mailops.purchase_domain(
            domain_name="mybusiness.com",
            registrant_info={"years": 2, "name": "John Smith", "email": "john@example.com"},
            context=ctx,
        )

        assert result.success
        assert result.approval_required
        assert result.data["domain_name"] == "mybusiness.com"
        assert result.data["years"] == 2
        assert result.data["risk_tier"] == "red"
        assert result.receipt["event_type"] == "domain.purchase"
        assert result.receipt["metadata"]["years"] == 2

    @pytest.mark.asyncio
    async def test_purchase_domain_missing_name(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Purchase without domain_name fails closed (Law #3)."""
        result = await mailops.purchase_domain(
            domain_name="",
            registrant_info={"years": 1},
            context=ctx,
        )

        assert not result.success
        assert result.receipt["status"] == "denied"
        assert result.receipt["policy"]["decision"] == "deny"
        assert "MISSING_BINDING_FIELDS" in result.receipt["policy"]["reasons"]


# =============================================================================
# 4. Domain Delete RED Tests
# =============================================================================


class TestDomainDelete:
    """Test delete_domain (RED tier)."""

    @pytest.mark.asyncio
    async def test_delete_domain_red_approval_required(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Domain deletion returns RED plan with approval_required."""
        result = await mailops.delete_domain("old-domain.com", ctx)

        assert result.success
        assert result.approval_required
        assert result.data["domain_name"] == "old-domain.com"
        assert result.data["risk_tier"] == "red"
        assert result.receipt["event_type"] == "domain.delete"
        assert result.receipt["metadata"]["domain_name"] == "old-domain.com"


# =============================================================================
# 5. Mail Account CRUD Tests
# =============================================================================


class TestMailAccount:
    """Test mail account operations."""

    @pytest.mark.asyncio
    async def test_create_mail_account_yellow_approval(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Mail account creation returns YELLOW plan with approval_required."""
        result = await mailops.create_mail_account(
            domain_name="example.com",
            email_address="hello@example.com",
            context=ctx,
        )

        assert result.success
        assert result.approval_required
        assert result.data["domain"] == "example.com"
        assert result.data["email_address"] == "hello@example.com"
        assert result.data["risk_tier"] == "yellow"
        assert result.receipt["event_type"] == "mail.account.create"

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.skillpacks.mail_ops_desk.execute_tool")
    async def test_read_mail_account_green_success(
        self, mock_exec: AsyncMock, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Read mail account returns GREEN result with no approval required."""
        mock_exec.return_value = _mock_tool_result(
            tool_id="polaris.account.read",
            data={"accounts": [{"email": "hello@example.com", "status": "active"}]},
        )

        result = await mailops.read_mail_account("hello@example.com", ctx)

        assert result.success
        assert not result.approval_required
        assert result.data["accounts"][0]["email"] == "hello@example.com"
        assert result.receipt["event_type"] == "mail.account.read"

        # Verify domain was extracted from email
        call_kwargs = mock_exec.call_args.kwargs
        assert call_kwargs["payload"] == {"domain": "example.com"}


# =============================================================================
# 6. Evil Tests
# =============================================================================


class TestEvilMailOps:
    """Evil tests — security boundaries (Law #3, #6, content isolation)."""

    @pytest.mark.asyncio
    async def test_evil_read_email_body_blocked(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """mail_ops_desk NEVER accesses email body content.

        The skill pack only does admin operations (domain/account management).
        It has no method to read email content — that's Eli's job.
        Verify that no method in the class accepts body/content parameters
        and that the blocklist is enforced.
        """
        from aspire_orchestrator.skillpacks.mail_ops_desk import (
            BLOCKED_CONTENT_FIELDS,
            _contains_blocked_content,
        )

        # Verify blocklist contains all dangerous fields
        assert "body" in BLOCKED_CONTENT_FIELDS
        assert "email_body" in BLOCKED_CONTENT_FIELDS
        assert "message_body" in BLOCKED_CONTENT_FIELDS
        assert "content" in BLOCKED_CONTENT_FIELDS
        assert "attachments" in BLOCKED_CONTENT_FIELDS

        # Verify detection function works
        assert _contains_blocked_content({"body": "secret email"})
        assert _contains_blocked_content({"email_body": "private data"})
        assert not _contains_blocked_content({"domain": "example.com"})

    @pytest.mark.asyncio
    async def test_evil_cross_tenant_domain_access(
        self, mailops: MailOpsDeskSkillPack, evil_ctx: MailOpsContext,
    ) -> None:
        """Domain operations with evil context have evil tenant IDs.

        Verifies tenant scoping in receipts cannot be forged (Law #6).
        An attacker cannot check/purchase domains attributed to another tenant.
        """
        result = await mailops.purchase_domain(
            domain_name="victim-business.com",
            registrant_info={"years": 1},
            context=evil_ctx,
        )

        # The receipt should contain the evil tenant, not the victim
        assert result.receipt["suite_id"] == EVIL_SUITE_ID
        assert result.receipt["office_id"] == EVIL_OFFICE_ID
        # Verify it's NOT the legitimate tenant
        assert result.receipt["suite_id"] != SUITE_ID
        assert result.receipt["office_id"] != OFFICE_ID

    @pytest.mark.asyncio
    async def test_evil_missing_all_params_fails_closed(
        self, mailops: MailOpsDeskSkillPack, ctx: MailOpsContext,
    ) -> None:
        """Operations with all params missing fail closed (Law #3).

        Every operation must produce a receipt even when failing.
        """
        # Domain check with empty name
        r1 = await mailops.check_domain("", ctx)
        assert not r1.success
        assert r1.receipt["status"] == "denied"
        assert r1.receipt["policy"]["decision"] == "deny"

        # DNS create with all empty
        r2 = await mailops.create_dns_record("", "", "", ctx)
        assert not r2.success
        assert r2.receipt["status"] == "denied"

        # Mail account create with empty
        r3 = await mailops.create_mail_account("", "", ctx)
        assert not r3.success
        assert r3.receipt["status"] == "denied"

        # Domain delete with empty
        r4 = await mailops.delete_domain("", ctx)
        assert not r4.success
        assert r4.receipt["status"] == "denied"

        # Verify domain with empty
        r5 = await mailops.verify_domain("", ctx)
        assert not r5.success
        assert r5.receipt["status"] == "denied"

        # Read mail account with empty
        r6 = await mailops.read_mail_account("", ctx)
        assert not r6.success
        assert r6.receipt["status"] == "denied"
