"""Control Plane Registry Tests — Wave 6.

Validates:
- YAML manifest loading (skill packs, tools, providers)
- Action → skill pack routing
- Capability discovery API
- Filtering (category, risk tier, status)
- Fail-closed on missing/malformed manifest (Law #3)
- Stats computation
"""

import pytest
from pathlib import Path

from aspire_orchestrator.models import RiskTier
from aspire_orchestrator.services.registry import (
    load_registry,
    get_registry,
    ControlPlaneRegistry,
    SkillPackManifest,
    ToolDefinition,
    ProviderDefinition,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def registry() -> ControlPlaneRegistry:
    """Load the production registry from config."""
    return load_registry()


# =============================================================================
# Loading Tests
# =============================================================================


class TestRegistryLoading:
    """Test YAML manifest loading and parsing."""

    def test_loads_production_manifest(self, registry: ControlPlaneRegistry):
        """Production manifest loads without error."""
        assert registry.version == "1.0.0"

    def test_loads_all_skill_packs(self, registry: ControlPlaneRegistry):
        """All 17 skill packs loaded (11 customer + 6 internal)."""
        assert len(registry.skill_packs) == 17

    def test_loads_all_tools(self, registry: ControlPlaneRegistry):
        """All tool definitions loaded."""
        assert len(registry.tools) >= 28  # 28+ tools in manifest

    def test_loads_all_providers(self, registry: ControlPlaneRegistry):
        """All provider definitions loaded."""
        assert len(registry.providers) >= 14  # 14+ providers in manifest

    def test_skill_pack_has_required_fields(self, registry: ControlPlaneRegistry):
        """Every skill pack has all required fields."""
        for pack_id, pack in registry.skill_packs.items():
            assert pack.id, f"{pack_id} missing id"
            assert pack.name, f"{pack_id} missing name"
            assert pack.owner, f"{pack_id} missing owner"
            assert pack.category, f"{pack_id} missing category"
            assert isinstance(pack.risk_tier, RiskTier), f"{pack_id} has invalid risk_tier"
            assert pack.status in ("registered", "active", "suspended", "development"), f"{pack_id} has invalid status"

    def test_tool_has_required_fields(self, registry: ControlPlaneRegistry):
        """Every tool has provider, category, and risk tier."""
        for tool_id, tool in registry.tools.items():
            assert tool.provider, f"Tool {tool_id} missing provider"
            assert tool.category, f"Tool {tool_id} missing category"
            assert isinstance(tool.risk_tier, RiskTier), f"Tool {tool_id} has invalid risk_tier"

    def test_provider_has_required_fields(self, registry: ControlPlaneRegistry):
        """Every provider has connection metadata."""
        for prov_id, prov in registry.providers.items():
            assert prov.name, f"Provider {prov_id} missing name"
            assert prov.auth_type, f"Provider {prov_id} missing auth_type"
            assert prov.timeout_ms > 0, f"Provider {prov_id} has invalid timeout"
            assert prov.rate_limit_rpm > 0, f"Provider {prov_id} has invalid rate limit"

    def test_fail_closed_missing_manifest(self):
        """FileNotFoundError raised for missing manifest (Law #3)."""
        with pytest.raises(FileNotFoundError, match="Fail-closed"):
            load_registry("/nonexistent/path.yaml")

    def test_singleton_caching(self):
        """get_registry returns cached singleton."""
        r1 = get_registry(reload=True)
        r2 = get_registry()
        assert r1 is r2


# =============================================================================
# Skill Pack Lookup Tests
# =============================================================================


class TestSkillPackLookup:
    """Test specific skill pack retrieval."""

    EXPECTED_PACKS = [
        ("sarah_front_desk", "sarah", "channel", RiskTier.YELLOW),
        ("eli_inbox", "eli", "channel", RiskTier.YELLOW),
        ("quinn_invoicing", "quinn", "channel", RiskTier.YELLOW),
        ("nora_conference", "nora", "channel", RiskTier.YELLOW),
        ("adam_research", "adam", "channel", RiskTier.GREEN),
        ("tec_documents", "tec", "channel", RiskTier.YELLOW),
        ("finn_finance_manager", "finn", "finance", RiskTier.YELLOW),
        ("milo_payroll", "milo", "finance", RiskTier.RED),
        ("teressa_books", "teressa", "finance", RiskTier.YELLOW),
        ("clara_legal", "clara", "legal", RiskTier.RED),
        ("mail_ops_desk", "mail_ops", "internal_admin", RiskTier.YELLOW),
    ]

    @pytest.mark.parametrize("pack_id,owner,category,risk_tier", EXPECTED_PACKS)
    def test_skill_pack_registered(
        self,
        registry: ControlPlaneRegistry,
        pack_id: str,
        owner: str,
        category: str,
        risk_tier: RiskTier,
    ):
        """Each expected skill pack is registered with correct metadata."""
        pack = registry.get_skill_pack(pack_id)
        assert pack is not None, f"Skill pack {pack_id} not found"
        assert pack.owner == owner
        assert pack.category == category
        assert pack.risk_tier == risk_tier
        assert pack.status in ("registered", "active")

    def test_unknown_pack_returns_none(self, registry: ControlPlaneRegistry):
        """Unknown pack ID returns None."""
        assert registry.get_skill_pack("nonexistent_pack") is None


# =============================================================================
# Action Routing Tests
# =============================================================================


class TestActionRouting:
    """Test action_type → skill pack routing."""

    EXPECTED_ROUTES = [
        ("email.send", "eli_inbox", "eli"),
        ("email.draft", "eli_inbox", "eli"),
        ("invoice.create", "quinn_invoicing", "quinn"),
        ("meeting.schedule", "nora_conference", "nora"),
        ("research.search", "adam_research", "adam"),
        ("payroll.run", "milo_payroll", "milo"),
        ("books.sync", "teressa_books", "teressa"),
        ("contract.generate", "clara_legal", "clara"),
        ("contract.sign", "clara_legal", "clara"),
        ("domain.check", "mail_ops_desk", "mail_ops"),
        ("domain.verify", "mail_ops_desk", "mail_ops"),
        ("domain.dns.create", "mail_ops_desk", "mail_ops"),
        ("domain.purchase", "mail_ops_desk", "mail_ops"),
        ("mail.account.create", "mail_ops_desk", "mail_ops"),
        ("mail.account.read", "mail_ops_desk", "mail_ops"),
    ]

    @pytest.mark.parametrize("action_type,expected_pack,expected_owner", EXPECTED_ROUTES)
    def test_routes_to_correct_skill_pack(
        self,
        registry: ControlPlaneRegistry,
        action_type: str,
        expected_pack: str,
        expected_owner: str,
    ):
        """Each action routes to the correct skill pack."""
        result = registry.route_action(action_type)
        assert result.found, f"Action {action_type} not routed"
        assert result.skill_pack_id == expected_pack
        assert result.owner == expected_owner

    def test_unknown_action_not_found(self, registry: ControlPlaneRegistry):
        """Unknown action returns found=False."""
        result = registry.route_action("hack.system")
        assert not result.found
        assert result.skill_pack_id is None

    def test_route_includes_tools(self, registry: ControlPlaneRegistry):
        """Routing result includes tool identifiers."""
        result = registry.route_action("invoice.create")
        assert result.found
        assert len(result.tools) > 0
        assert "stripe.invoice.create" in result.tools

    def test_route_includes_providers(self, registry: ControlPlaneRegistry):
        """Routing result includes provider names."""
        result = registry.route_action("invoice.create")
        assert result.found
        assert "stripe" in result.providers


# =============================================================================
# Filtering Tests
# =============================================================================


class TestFiltering:
    """Test skill pack list filtering."""

    def test_filter_by_category(self, registry: ControlPlaneRegistry):
        """Filter by category returns correct subset."""
        channel_packs = registry.list_skill_packs(category="channel")
        assert len(channel_packs) == 7

        finance_packs = registry.list_skill_packs(category="finance")
        assert len(finance_packs) == 3

        legal_packs = registry.list_skill_packs(category="legal")
        assert len(legal_packs) == 1

        internal_packs = registry.list_skill_packs(category="internal")
        assert len(internal_packs) == 4

    def test_filter_by_risk_tier(self, registry: ControlPlaneRegistry):
        """Filter by risk tier returns correct subset."""
        green_packs = registry.list_skill_packs(risk_tier=RiskTier.GREEN)
        assert len(green_packs) == 3  # adam, ava_user, qa_evals

        red_packs = registry.list_skill_packs(risk_tier=RiskTier.RED)
        assert len(red_packs) == 3  # milo, clara, security_review

        yellow_packs = registry.list_skill_packs(risk_tier=RiskTier.YELLOW)
        assert len(yellow_packs) == 11

    def test_filter_by_status(self, registry: ControlPlaneRegistry):
        """All packs are in 'active' status after Phase 1 deployment."""
        active = registry.list_skill_packs(status="active")
        assert len(active) == 17

    def test_combined_filters(self, registry: ControlPlaneRegistry):
        """Multiple filters combine correctly."""
        result = registry.list_skill_packs(category="finance", risk_tier=RiskTier.RED)
        assert len(result) == 1  # milo only

    def test_no_match_returns_empty(self, registry: ControlPlaneRegistry):
        """Filters with no matches return empty list."""
        result = registry.list_skill_packs(category="nonexistent")
        assert len(result) == 0


# =============================================================================
# Capability Discovery Tests
# =============================================================================


class TestCapabilityDiscovery:
    """Test list_capabilities for the discovery API."""

    def test_lists_all_capabilities(self, registry: ControlPlaneRegistry):
        """list_capabilities returns all registered skill packs."""
        caps = registry.list_capabilities()
        assert len(caps) == 17

    def test_capability_has_required_fields(self, registry: ControlPlaneRegistry):
        """Each capability entry has the required discovery fields."""
        caps = registry.list_capabilities()
        for cap in caps:
            assert "skill_pack_id" in cap
            assert "name" in cap
            assert "owner" in cap
            assert "category" in cap
            assert "risk_tier" in cap
            assert "status" in cap
            assert "actions" in cap
            assert "tools" in cap

    def test_risk_tiers_are_string_values(self, registry: ControlPlaneRegistry):
        """Risk tiers in discovery are string values, not enums."""
        caps = registry.list_capabilities()
        for cap in caps:
            assert cap["risk_tier"] in ("green", "yellow", "red")


# =============================================================================
# Stats Tests
# =============================================================================


class TestStats:
    """Test registry statistics."""

    def test_stats_totals(self, registry: ControlPlaneRegistry):
        """Stats include correct totals."""
        stats = registry.get_stats()
        assert stats["total_skill_packs"] == 17
        assert stats["total_tools"] >= 33
        assert stats["total_providers"] >= 14
        assert stats["total_actions_mapped"] >= 17

    def test_stats_by_category(self, registry: ControlPlaneRegistry):
        """Stats by category match expectations."""
        stats = registry.get_stats()
        by_cat = stats["by_category"]
        assert by_cat.get("channel") == 7
        assert by_cat.get("finance") == 3
        assert by_cat.get("legal") == 1
        assert by_cat.get("internal") == 4
        assert by_cat.get("internal_admin") == 2

    def test_stats_by_risk_tier(self, registry: ControlPlaneRegistry):
        """Stats by risk tier match expectations."""
        stats = registry.get_stats()
        by_risk = stats["by_risk_tier"]
        assert by_risk.get("green") == 3
        assert by_risk.get("yellow") == 11
        assert by_risk.get("red") == 3


# =============================================================================
# Tool/Provider Lookup Tests
# =============================================================================


class TestToolProviderLookup:
    """Test individual tool and provider lookups."""

    def test_get_known_tool(self, registry: ControlPlaneRegistry):
        """Known tool returns correct definition."""
        tool = registry.get_tool("stripe.invoice.create")
        assert tool is not None
        assert tool.provider == "stripe"
        assert tool.risk_tier == RiskTier.YELLOW

    def test_get_unknown_tool(self, registry: ControlPlaneRegistry):
        """Unknown tool returns None."""
        assert registry.get_tool("nonexistent.tool") is None

    def test_get_known_provider(self, registry: ControlPlaneRegistry):
        """Known provider returns correct definition."""
        prov = registry.get_provider("stripe")
        assert prov is not None
        assert prov.auth_type == "api_key"
        assert prov.idempotency_support is True
        assert prov.timeout_ms == 10000

    def test_get_unknown_provider(self, registry: ControlPlaneRegistry):
        """Unknown provider returns None."""
        assert registry.get_provider("nonexistent") is None

    def test_red_tier_tools_match_manifest(self, registry: ControlPlaneRegistry):
        """RED tier tools are correctly classified."""
        red_tools = [
            "pandadoc.contract.sign",
            "gusto.payroll.run",
            "tax.filing.submit",
            "domain.purchase",
            "domain.delete",
            "data.delete",
        ]
        for tool_id in red_tools:
            tool = registry.get_tool(tool_id)
            assert tool is not None, f"Tool {tool_id} not found"
            assert tool.risk_tier == RiskTier.RED, f"Tool {tool_id} should be RED, got {tool.risk_tier}"
