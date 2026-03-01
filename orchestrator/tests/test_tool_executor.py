"""Tool Executor Registry Tests — Wave 7.

Validates:
- Tool routing: Domain Rail tools → live executors, others → stub
- 7 Domain Rail executor functions with receipt emission (Law #2)
- Missing parameter handling (fail-closed, Law #3)
- S2S client error propagation
- Stub executor produces correct receipt data
- Risk tier verification for mail/domain tools (Law #4)
- Execute node integration with tool executor registry

Cross-reference:
  - skill_pack_manifests.yaml for tool → skill pack mapping
  - policy_matrix.yaml for action → risk tier mapping
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aspire_orchestrator.models import Outcome, RiskTier
from aspire_orchestrator.services.tool_executor import (
    ToolExecutionResult,
    execute_tool,
    execute_stub,
    execute_domain_check,
    execute_domain_verify,
    execute_domain_dns_create,
    execute_domain_purchase,
    execute_domain_delete,
    execute_mail_account_create,
    execute_mail_account_read,
    get_live_tools,
    is_live_tool,
)
from aspire_orchestrator.services.domain_rail_client import (
    DomainRailClientError,
    DomainRailResponse,
)
from aspire_orchestrator.services.registry import get_registry


# =============================================================================
# Test Constants
# =============================================================================

SUITE_ID = "suite-test-001"
OFFICE_ID = "office-test-001"
CORRELATION_ID = "corr-wave7-002"
CAP_TOKEN_ID = "token-001"
CAP_TOKEN_HASH = "hash-001"
BASE_KWARGS = {
    "correlation_id": CORRELATION_ID,
    "suite_id": SUITE_ID,
    "office_id": OFFICE_ID,
    "capability_token_id": CAP_TOKEN_ID,
    "capability_token_hash": CAP_TOKEN_HASH,
}


# =============================================================================
# Tool Registry Tests
# =============================================================================


class TestToolRegistry:
    """Verify tool routing to live vs stub executors."""

    def test_live_tools_list(self):
        """Domain Rail + Phase 2 provider tools are registered as live."""
        live = get_live_tools()
        # 7 Domain Rail + 2 Search + 2 Invoicing = 11 live executors
        assert len(live) >= 11
        # Domain Rail
        assert "domain.check" in live
        assert "domain.verify" in live
        assert "domain.dns.create" in live
        assert "domain.purchase" in live
        assert "domain.delete" in live
        assert "polaris.account.create" in live
        assert "polaris.account.read" in live
        # Phase 2: Search
        assert "brave.search" in live
        assert "tavily.search" in live
        # Phase 2: Invoicing
        assert "stripe.invoice.create" in live
        assert "stripe.invoice.send" in live

    def test_is_live_tool_true(self):
        """Domain Rail tools report as live."""
        assert is_live_tool("domain.check") is True
        assert is_live_tool("domain.purchase") is True
        assert is_live_tool("polaris.account.create") is True

    def test_is_live_tool_false(self):
        """Unimplemented tools report as stub."""
        assert is_live_tool("slack.message.send") is False
        assert is_live_tool("slack.message.send") is False
        assert is_live_tool("nonexistent.tool") is False

    def test_live_tools_match_manifest(self):
        """Live tools match mail_ops_desk manifest tools list."""
        registry = get_registry(reload=True)
        mail_ops = registry.get_skill_pack("mail_ops_desk")
        assert mail_ops is not None

        live = set(get_live_tools())
        manifest_tools = set(mail_ops.tools)

        # All manifest tools should be live
        assert manifest_tools.issubset(live), (
            f"Manifest tools not all live: {manifest_tools - live}"
        )


# =============================================================================
# Mail Risk Tier Verification
# =============================================================================


class TestMailRiskTiers:
    """Verify Domain Rail tools have correct risk tiers (Law #4)."""

    def test_green_tier_tools(self):
        """Read-only tools are GREEN."""
        registry = get_registry(reload=True)
        green_tools = ["domain.check", "domain.verify", "polaris.account.read"]
        for tool_id in green_tools:
            tool = registry.get_tool(tool_id)
            assert tool is not None, f"Tool {tool_id} not found"
            assert tool.risk_tier == RiskTier.GREEN, (
                f"Tool {tool_id} should be GREEN, got {tool.risk_tier}"
            )

    def test_yellow_tier_tools(self):
        """Write tools are YELLOW."""
        registry = get_registry(reload=True)
        yellow_tools = ["domain.dns.create", "polaris.account.create"]
        for tool_id in yellow_tools:
            tool = registry.get_tool(tool_id)
            assert tool is not None, f"Tool {tool_id} not found"
            assert tool.risk_tier == RiskTier.YELLOW, (
                f"Tool {tool_id} should be YELLOW, got {tool.risk_tier}"
            )

    def test_red_tier_tools(self):
        """Financial/irreversible tools are RED."""
        registry = get_registry(reload=True)
        red_tools = ["domain.purchase", "domain.delete"]
        for tool_id in red_tools:
            tool = registry.get_tool(tool_id)
            assert tool is not None, f"Tool {tool_id} not found"
            assert tool.risk_tier == RiskTier.RED, (
                f"Tool {tool_id} should be RED, got {tool.risk_tier}"
            )

    def test_provider_is_resellerclub(self):
        """Domain tools use ResellerClub provider."""
        registry = get_registry(reload=True)
        for tool_id in ["domain.check", "domain.verify", "domain.dns.create",
                        "domain.purchase", "domain.delete"]:
            tool = registry.get_tool(tool_id)
            assert tool is not None
            assert tool.provider == "resellerclub"

    def test_mail_provider_is_polarism(self):
        """Mail tools use PolarisM provider."""
        registry = get_registry(reload=True)
        for tool_id in ["polaris.account.create", "polaris.account.read"]:
            tool = registry.get_tool(tool_id)
            assert tool is not None
            assert tool.provider == "polarism"


# =============================================================================
# Capability Scope Verification
# =============================================================================


class TestMailCapabilityScopes:
    """Verify mail/domain capability token scopes are registered."""

    def test_mail_ops_scopes(self):
        """mail_ops_desk has all required capability scopes."""
        registry = get_registry(reload=True)
        pack = registry.get_skill_pack("mail_ops_desk")
        assert pack is not None
        expected_scopes = [
            "domain:read", "domain:dns:write", "domain:purchase",
            "domain:delete", "mail:account:read", "mail:account:write",
        ]
        for scope in expected_scopes:
            assert scope in pack.capability_scopes, (
                f"Scope {scope} missing from mail_ops_desk"
            )

    def test_mail_ops_actions(self):
        """mail_ops_desk handles all 7 mail/domain actions."""
        registry = get_registry(reload=True)
        pack = registry.get_skill_pack("mail_ops_desk")
        assert pack is not None
        expected_actions = [
            "domain.check", "domain.verify", "domain.dns.create",
            "domain.purchase", "domain.delete",
            "mail.account.create", "mail.account.read",
        ]
        for action in expected_actions:
            assert action in pack.actions, (
                f"Action {action} missing from mail_ops_desk"
            )


# =============================================================================
# Stub Executor Tests
# =============================================================================


class TestStubExecutor:
    """Verify stub executor for non-Domain Rail tools."""

    @pytest.mark.asyncio
    async def test_returns_success(self):
        """Stub executor always returns success."""
        result = await execute_stub(
            tool_id="stripe.invoice.create",
            payload={"customer_id": "cust-001"},
            **BASE_KWARGS,
        )
        assert result.outcome == Outcome.SUCCESS
        assert result.is_stub is True

    @pytest.mark.asyncio
    async def test_includes_receipt(self):
        """Stub executor emits receipt data (Law #2)."""
        result = await execute_stub(
            tool_id="gusto.payroll.run",
            payload={},
            **BASE_KWARGS,
        )
        assert result.receipt_data
        assert result.receipt_data["tool_used"] == "gusto.payroll.run"
        assert result.receipt_data["outcome"] == "success"
        assert result.receipt_data["reason_code"] == "EXECUTED_STUB"
        assert result.receipt_data["suite_id"] == SUITE_ID

    @pytest.mark.asyncio
    async def test_stub_data_includes_marker(self):
        """Stub result data includes stub=True marker."""
        result = await execute_stub(
            tool_id="pandadoc.contract.generate",
            payload={},
            **BASE_KWARGS,
        )
        assert result.data["stub"] is True
        assert result.data["tool"] == "pandadoc.contract.generate"


# =============================================================================
# Domain Rail Executor Tests (mocked HTTP)
# =============================================================================


class TestDomainRailExecutors:
    """Test individual Domain Rail executor functions."""

    @pytest.fixture(autouse=True)
    def mock_settings(self, monkeypatch):
        """Mock settings for Domain Rail client."""
        monkeypatch.setattr(
            "aspire_orchestrator.services.domain_rail_client.settings",
            MagicMock(
                s2s_hmac_secret="test-secret",
                domain_rail_url="http://localhost:3000",
            ),
        )

    @pytest.mark.asyncio
    async def test_domain_check_success(self):
        """domain.check returns availability with receipt."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"available": True}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mc:
            inst = AsyncMock()
            inst.get.return_value = mock_resp
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mc.return_value = inst

            result = await execute_domain_check(
                payload={"domain": "test.com"},
                **BASE_KWARGS,
            )
            assert result.outcome == Outcome.SUCCESS
            assert result.receipt_data["tool_used"] == "domain.check"
            assert result.receipt_data["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_domain_check_missing_param(self):
        """domain.check fails without domain parameter."""
        result = await execute_domain_check(
            payload={},
            **BASE_KWARGS,
        )
        assert result.outcome == Outcome.FAILED
        assert "domain" in result.error.lower()
        assert result.receipt_data["reason_code"] == "MISSING_DOMAIN_PARAM"

    @pytest.mark.asyncio
    async def test_domain_dns_create_success(self):
        """domain.dns.create creates DNS record with receipt."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"created": True}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mc:
            inst = AsyncMock()
            inst.post.return_value = mock_resp
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mc.return_value = inst

            result = await execute_domain_dns_create(
                payload={"domain": "test.com", "record_type": "A", "value": "1.2.3.4"},
                risk_tier="yellow",
                **BASE_KWARGS,
            )
            assert result.outcome == Outcome.SUCCESS

    @pytest.mark.asyncio
    async def test_domain_dns_create_missing_params(self):
        """domain.dns.create fails without all required params."""
        result = await execute_domain_dns_create(
            payload={"domain": "test.com"},  # Missing record_type, value
            **BASE_KWARGS,
        )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data["reason_code"] == "MISSING_PARAMS"

    @pytest.mark.asyncio
    async def test_domain_purchase_success(self):
        """domain.purchase creates receipt with RED tier."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"purchased": True}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mc:
            inst = AsyncMock()
            inst.post.return_value = mock_resp
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mc.return_value = inst

            result = await execute_domain_purchase(
                payload={"domain_name": "new.com", "years": 1},
                risk_tier="red",
                **BASE_KWARGS,
            )
            assert result.outcome == Outcome.SUCCESS
            assert result.receipt_data["risk_tier"] == "red"

    @pytest.mark.asyncio
    async def test_domain_purchase_missing_name(self):
        """domain.purchase fails without domain_name."""
        result = await execute_domain_purchase(
            payload={},
            **BASE_KWARGS,
        )
        assert result.outcome == Outcome.FAILED

    @pytest.mark.asyncio
    async def test_domain_delete_success(self):
        """domain.delete completes with receipt."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"deleted": True}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mc:
            inst = AsyncMock()
            inst.delete.return_value = mock_resp
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mc.return_value = inst

            result = await execute_domain_delete(
                payload={"domain": "old.com"},
                **BASE_KWARGS,
            )
            assert result.outcome == Outcome.SUCCESS

    @pytest.mark.asyncio
    async def test_mail_account_create_success(self):
        """polaris.account.create creates account with receipt."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"created": True}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mc:
            inst = AsyncMock()
            inst.post.return_value = mock_resp
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mc.return_value = inst

            result = await execute_mail_account_create(
                payload={"domain": "test.com", "email_address": "info@test.com", "display_name": "Info"},
                **BASE_KWARGS,
            )
            assert result.outcome == Outcome.SUCCESS

    @pytest.mark.asyncio
    async def test_mail_account_create_missing_params(self):
        """polaris.account.create fails without required params."""
        result = await execute_mail_account_create(
            payload={"domain": "test.com"},  # Missing email_address
            **BASE_KWARGS,
        )
        assert result.outcome == Outcome.FAILED

    @pytest.mark.asyncio
    async def test_mail_account_read_success(self):
        """polaris.account.read lists accounts with receipt."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"accounts": []}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mc:
            inst = AsyncMock()
            inst.get.return_value = mock_resp
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mc.return_value = inst

            result = await execute_mail_account_read(
                payload={"domain": "test.com"},
                **BASE_KWARGS,
            )
            assert result.outcome == Outcome.SUCCESS

    @pytest.mark.asyncio
    async def test_s2s_error_propagated(self):
        """S2S client errors produce failed result with receipt."""
        with patch(
            "aspire_orchestrator.services.tool_executor.domain_check",
            side_effect=DomainRailClientError("S2S_SECRET_MISSING", "No secret"),
        ):
            result = await execute_domain_check(
                payload={"domain": "test.com"},
                **BASE_KWARGS,
            )
            assert result.outcome == Outcome.FAILED
            assert result.receipt_data["reason_code"] == "S2S_SECRET_MISSING"


# =============================================================================
# execute_tool Routing Tests
# =============================================================================


class TestExecuteToolRouting:
    """Test the top-level execute_tool function routing."""

    @pytest.fixture(autouse=True)
    def mock_settings(self, monkeypatch):
        """Mock settings for Domain Rail client."""
        monkeypatch.setattr(
            "aspire_orchestrator.services.domain_rail_client.settings",
            MagicMock(
                s2s_hmac_secret="test-secret",
                domain_rail_url="http://localhost:3000",
            ),
        )

    @pytest.mark.asyncio
    async def test_routes_to_live_executor(self):
        """Domain Rail tools route to live executors."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"available": True}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mc:
            inst = AsyncMock()
            inst.get.return_value = mock_resp
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mc.return_value = inst

            result = await execute_tool(
                tool_id="domain.check",
                payload={"domain": "test.com"},
                **BASE_KWARGS,
            )
            assert result.outcome == Outcome.SUCCESS
            assert result.is_stub is False

    @pytest.mark.asyncio
    async def test_routes_to_stub_executor(self):
        """Unimplemented tools route to stub."""
        result = await execute_tool(
            tool_id="slack.message.send",
            payload={"channel": "general", "text": "hello"},
            **BASE_KWARGS,
        )
        assert result.outcome == Outcome.SUCCESS
        assert result.is_stub is True

    @pytest.mark.asyncio
    async def test_unknown_tool_uses_stub(self):
        """Completely unknown tools get stub executor."""
        result = await execute_tool(
            tool_id="nonexistent.tool.action",
            payload={},
            **BASE_KWARGS,
        )
        assert result.outcome == Outcome.SUCCESS
        assert result.is_stub is True

    @pytest.mark.asyncio
    async def test_all_live_tools_have_receipt(self):
        """Every live tool produces receipt data on execution."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}

        # Mock Supabase for calendar tools
        mock_supabase_insert = AsyncMock(return_value={"id": "test-event-id"})
        mock_supabase_select = AsyncMock(return_value=[])
        mock_supabase_update = AsyncMock(return_value={"status": "completed"})

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mc, \
             patch("aspire_orchestrator.providers.calendar_client.supabase_insert", mock_supabase_insert, create=True), \
             patch("aspire_orchestrator.services.supabase_client.supabase_insert", mock_supabase_insert, create=True), \
             patch("aspire_orchestrator.services.supabase_client.supabase_select", mock_supabase_select, create=True), \
             patch("aspire_orchestrator.services.supabase_client.supabase_update", mock_supabase_update, create=True):
            inst = AsyncMock()
            inst.get.return_value = mock_resp
            inst.post.return_value = mock_resp
            inst.delete.return_value = mock_resp
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mc.return_value = inst

            for tool_id in get_live_tools():
                # Build minimal payload for each tool
                if tool_id == "domain.dns.create":
                    payload = {"domain": "t.com", "record_type": "A", "value": "1.2.3.4"}
                elif tool_id in ("domain.purchase",):
                    payload = {"domain_name": "t.com", "years": 1}
                elif tool_id == "polaris.account.create":
                    payload = {"domain": "t.com", "email_address": "a@t.com"}
                elif tool_id == "calendar.event.complete":
                    payload = {"event_id": "test-event-id"}
                elif tool_id.startswith("calendar."):
                    payload = {"title": "Test Event", "start_time": "2026-02-20T10:00:00Z"}
                else:
                    payload = {"domain": "t.com"}

                result = await execute_tool(
                    tool_id=tool_id,
                    payload=payload,
                    **BASE_KWARGS,
                )
                assert result.receipt_data, f"Tool {tool_id} missing receipt"
                assert result.receipt_data["suite_id"] == SUITE_ID
                assert result.receipt_data["correlation_id"] == CORRELATION_ID


# =============================================================================
# Execute Node Integration Tests
# =============================================================================


class TestExecuteNodeIntegration:
    """Test execute node uses tool executor registry.

    Execute node now performs full 6-check token validation (P0 fix).
    Tests must provide real tokens minted via token_service.mint_token().
    """

    @staticmethod
    def _derive_scope(task_type: str) -> str:
        """Derive the required scope from task_type (matches token_mint + execute node)."""
        verb = task_type.split(".")[-1] if "." in task_type else task_type
        scope_map = {
            "read": "read", "list": "read", "search": "read",
            "create": "write", "send": "write", "draft": "write",
            "schedule": "write", "sign": "write", "transfer": "write",
            "delete": "delete", "purchase": "write",
        }
        scope_verb = scope_map.get(verb, "execute")
        domain = task_type.split(".")[0] if "." in task_type else task_type
        return f"{domain}.{scope_verb}"

    @staticmethod
    def _mint_test_token(
        suite_id: str = SUITE_ID,
        office_id: str = OFFICE_ID,
        tool: str = "domain.check",
        task_type: str = "domain.check",
        correlation_id: str = CORRELATION_ID,
    ) -> dict:
        """Mint a valid capability token for test state."""
        from aspire_orchestrator.services.token_service import mint_token

        scope = TestExecuteNodeIntegration._derive_scope(task_type)
        token = mint_token(
            suite_id=suite_id,
            office_id=office_id,
            tool=tool,
            scopes=[scope],
            correlation_id=correlation_id,
        )
        return token

    @pytest.mark.asyncio
    async def test_live_tool_flagged(self):
        """Execute node marks Domain Rail tools as live."""
        from aspire_orchestrator.nodes.execute import execute_node
        from aspire_orchestrator.services.token_service import compute_token_hash

        token = self._mint_test_token(tool="domain.check", task_type="domain.check")
        state = {
            "correlation_id": CORRELATION_ID,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "task_type": "domain.check",
            "allowed_tools": ["domain.check"],
            "capability_token_id": token["token_id"],
            "capability_token_hash": compute_token_hash(token),
            "capability_token": token,
            "risk_tier": "green",
            "pipeline_receipts": [],
        }

        result = await execute_node(state)
        assert result["outcome"] == Outcome.SUCCESS
        assert result["execution_result"]["live"] is True
        assert result["execution_result"]["stub"] is False

    @pytest.mark.asyncio
    async def test_stub_tool_flagged(self):
        """Execute node marks unimplemented tools as stub."""
        from aspire_orchestrator.nodes.execute import execute_node
        from aspire_orchestrator.services.token_service import compute_token_hash

        token = self._mint_test_token(
            tool="slack.message.send", task_type="message.send",
        )
        state = {
            "correlation_id": CORRELATION_ID,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "task_type": "message.send",
            "allowed_tools": ["slack.message.send"],
            "capability_token_id": token["token_id"],
            "capability_token_hash": compute_token_hash(token),
            "capability_token": token,
            "risk_tier": "yellow",
            "pipeline_receipts": [],
        }

        result = await execute_node(state)
        assert result["outcome"] == Outcome.SUCCESS
        assert result["execution_result"]["stub"] is True
        assert result["execution_result"]["live"] is False

    @pytest.mark.asyncio
    async def test_missing_token_denied(self):
        """Execute node denies without capability token (Law #3)."""
        from aspire_orchestrator.nodes.execute import execute_node

        state = {
            "correlation_id": CORRELATION_ID,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "task_type": "domain.check",
            "allowed_tools": ["domain.check"],
            "capability_token_id": None,
            "capability_token": None,
            "risk_tier": "green",
            "pipeline_receipts": [],
        }

        result = await execute_node(state)
        assert result["outcome"] == Outcome.DENIED
        assert result["error_code"] == "CAPABILITY_TOKEN_REQUIRED"

    @pytest.mark.asyncio
    async def test_invalid_token_denied(self):
        """Execute node denies with tampered token (6-check validation)."""
        from aspire_orchestrator.nodes.execute import execute_node

        token = self._mint_test_token(tool="domain.check", task_type="domain.check")
        # Tamper with the token signature
        token["signature"] = "tampered_" + token["signature"][9:]

        state = {
            "correlation_id": CORRELATION_ID,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "task_type": "domain.check",
            "allowed_tools": ["domain.check"],
            "capability_token_id": token["token_id"],
            "capability_token_hash": "tampered",
            "capability_token": token,
            "risk_tier": "green",
            "pipeline_receipts": [],
        }

        result = await execute_node(state)
        assert result["outcome"] == Outcome.DENIED
        assert result["error_code"] == "CAPABILITY_TOKEN_REQUIRED"

    @pytest.mark.asyncio
    async def test_cross_tenant_token_denied(self):
        """Execute node denies token minted for different suite (Law #6)."""
        from aspire_orchestrator.nodes.execute import execute_node
        from aspire_orchestrator.services.token_service import compute_token_hash

        # Mint token for a DIFFERENT suite
        token = self._mint_test_token(
            suite_id="other-suite-999",
            tool="domain.check",
            task_type="domain.check",
        )
        state = {
            "correlation_id": CORRELATION_ID,
            "suite_id": SUITE_ID,  # Request context has different suite
            "office_id": OFFICE_ID,
            "task_type": "domain.check",
            "allowed_tools": ["domain.check"],
            "capability_token_id": token["token_id"],
            "capability_token_hash": compute_token_hash(token),
            "capability_token": token,
            "risk_tier": "green",
            "pipeline_receipts": [],
        }

        result = await execute_node(state)
        assert result["outcome"] == Outcome.DENIED

    @pytest.mark.asyncio
    async def test_receipt_emitted(self):
        """Execute node emits A2A + tool execution receipts (Law #2).

        A2A wiring produces 4 receipts per execution:
          1. a2a.dispatch (Ava delegates to agent)
          2. a2a.claim (agent claims task)
          3. tool_execution (actual tool call with agent identity)
          4. a2a.complete (agent reports done)
        """
        from aspire_orchestrator.nodes.execute import execute_node
        from aspire_orchestrator.services.token_service import compute_token_hash

        token = self._mint_test_token(
            tool="domain.verify", task_type="domain.verify",
        )
        state = {
            "correlation_id": CORRELATION_ID,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "task_type": "domain.verify",
            "allowed_tools": ["domain.verify"],
            "capability_token_id": token["token_id"],
            "capability_token_hash": compute_token_hash(token),
            "capability_token": token,
            "risk_tier": "green",
            "pipeline_receipts": [],
        }

        result = await execute_node(state)
        receipts = result["pipeline_receipts"]
        # A2A lifecycle: dispatch + claim + execution + complete
        assert len(receipts) == 4
        assert receipts[0]["action_type"] == "a2a.dispatch"
        assert receipts[1]["action_type"] == "a2a.claim"
        # Tool execution receipt (the actual work)
        assert receipts[2]["tool_used"] == "domain.verify"
        assert receipts[2]["outcome"] == "success"
        assert receipts[2]["suite_id"] == SUITE_ID
        assert receipts[2]["actor_type"] == "agent"
        assert receipts[3]["action_type"] == "a2a.complete"
