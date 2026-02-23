"""Clara Legal Production Quality Tests — Full 22-template + enterprise hardening.

Covers:
  - All 22 template types from registry (parametrized)
  - Legacy alias resolution (nda -> general_mutual_nda)
  - Jurisdiction enforcement per template
  - Preflight validation (required_fields_delta)
  - Risk tier overrides (landlord_commercial_sublease = RED)
  - Webhook handler (HMAC verification, idempotency, state mapping)
  - Contract outbox (persistence, idempotency, tenant isolation)
  - Contract state machine edge cases
  - Evil tests (cross-tenant, forged token, template injection, HMAC forgery)

Law coverage:
  - Law #2: Every test verifies receipt generation
  - Law #3: Missing fields, bad templates, bad HMAC -> deny with receipt
  - Law #4: Risk tiers enforced per template registry
  - Law #6: Cross-tenant isolation throughout
  - Law #7: Tools execute only, never decide
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.clara_legal import (
    ACTOR_CLARA,
    VALID_TEMPLATE_TYPES,
    ClaraContext,
    ClaraLegalSkillPack,
    get_template_spec,
    preflight_validate,
    _resolve_template_key,
    _TEMPLATE_REGISTRY,
)
from aspire_orchestrator.providers.pandadoc_webhook import (
    PandaDocWebhookHandler,
    WebhookSignatureError,
    WebhookDuplicateError,
    verify_pandadoc_signature,
    map_pandadoc_status_to_state,
)
from aspire_orchestrator.services.contract_outbox import (
    ContractOutbox,
    ContractRecord,
    get_contract_outbox,
)
from aspire_orchestrator.services.contract_state_machine import (
    ContractStateMachine,
    InvalidTransitionError,
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


def _mock_response(
    success: bool,
    status_code: int = 200,
    body: dict | None = None,
    error_message: str | None = None,
):
    """Create a mock ProviderResponse for PandaDoc client tests."""
    from aspire_orchestrator.providers.base_client import ProviderResponse

    return ProviderResponse(
        status_code=status_code,
        body=body or {},
        success=success,
        error_message=error_message,
    )


@pytest.fixture
def clara() -> ClaraLegalSkillPack:
    return ClaraLegalSkillPack()


@pytest.fixture
def webhook_handler() -> PandaDocWebhookHandler:
    handler = PandaDocWebhookHandler(webhook_secret="test-secret-123")
    yield handler
    handler.clear_processed()


@pytest.fixture
def outbox() -> ContractOutbox:
    ob = ContractOutbox()
    yield ob
    ob.clear_store()


# ===========================================================================
# Template Registry Tests
# ===========================================================================

class TestTemplateRegistry:
    """Verify the 14 real PandaDoc template registry loads and resolves correctly."""

    def test_registry_loaded_14_templates(self) -> None:
        """Registry should contain exactly 14 templates (real PandaDoc UUIDs only)."""
        assert len(_TEMPLATE_REGISTRY) == 14, (
            f"Expected 14 templates, got {len(_TEMPLATE_REGISTRY)}: "
            f"{sorted(_TEMPLATE_REGISTRY.keys())}"
        )

    def test_all_lanes_present(self) -> None:
        """Registry should have templates from all 4 lanes."""
        lanes = {spec.get("lane") for spec in _TEMPLATE_REGISTRY.values()}
        assert lanes == {"trades", "accounting", "landlord", "general"}, (
            f"Missing lanes: expected 4, got {lanes}"
        )

    def test_trades_lane_has_8_templates(self) -> None:
        """Trades lane should have exactly 8 real templates."""
        trades = [k for k, v in _TEMPLATE_REGISTRY.items() if v.get("lane") == "trades"]
        assert len(trades) == 8, f"Expected 8 trades templates, got {len(trades)}: {trades}"

    def test_accounting_lane_has_2_templates(self) -> None:
        """Accounting lane should have exactly 2 real templates."""
        acct = [k for k, v in _TEMPLATE_REGISTRY.items() if v.get("lane") == "accounting"]
        assert len(acct) == 2, f"Expected 2 accounting templates, got {len(acct)}: {acct}"

    def test_landlord_lane_has_1_template(self) -> None:
        """Landlord lane should have exactly 1 real template."""
        landlord = [k for k, v in _TEMPLATE_REGISTRY.items() if v.get("lane") == "landlord"]
        assert len(landlord) == 1, f"Expected 1 landlord template, got {len(landlord)}: {landlord}"

    def test_general_lane_has_3_templates(self) -> None:
        """General lane should have exactly 3 real templates."""
        general = [k for k, v in _TEMPLATE_REGISTRY.items() if v.get("lane") == "general"]
        assert len(general) == 3, f"Expected 3 general templates, got {len(general)}: {general}"

    def test_legacy_alias_nda(self) -> None:
        """Legacy alias 'nda' should resolve to 'general_mutual_nda'."""
        assert _resolve_template_key("nda") == "general_mutual_nda"

    def test_legacy_alias_msa_retired(self) -> None:
        """Legacy alias 'msa' is retired — not in VALID_TEMPLATE_TYPES."""
        assert "msa" not in VALID_TEMPLATE_TYPES
        assert get_template_spec("msa") is None

    def test_legacy_alias_sow(self) -> None:
        """Legacy alias 'sow' should resolve to 'trades_sow'."""
        assert _resolve_template_key("sow") == "trades_sow"

    def test_legacy_alias_employment_retired(self) -> None:
        """Legacy alias 'employment' is retired — not in VALID_TEMPLATE_TYPES."""
        assert "employment" not in VALID_TEMPLATE_TYPES
        assert get_template_spec("employment") is None

    def test_legacy_alias_amendment_retired(self) -> None:
        """Legacy alias 'amendment' is retired — not in VALID_TEMPLATE_TYPES."""
        assert "amendment" not in VALID_TEMPLATE_TYPES
        assert get_template_spec("amendment") is None

    def test_get_template_spec_by_key(self) -> None:
        """get_template_spec should return spec for valid key."""
        spec = get_template_spec("general_mutual_nda")
        assert spec is not None
        assert spec["lane"] == "general"
        assert spec["risk_tier"] == "yellow"
        assert spec["jurisdiction_required"] is True

    def test_get_template_spec_by_alias(self) -> None:
        """get_template_spec should resolve legacy aliases."""
        spec = get_template_spec("nda")
        assert spec is not None
        assert spec["lane"] == "general"

    def test_get_template_spec_invalid(self) -> None:
        """get_template_spec should return None for invalid key."""
        assert get_template_spec("nonexistent_template") is None

    def test_commercial_sublease_is_red_tier(self) -> None:
        """landlord_commercial_sublease should be RED tier."""
        spec = get_template_spec("landlord_commercial_sublease")
        assert spec is not None
        assert spec["risk_tier"] == "red"

    def test_tax_filing_is_red_tier(self) -> None:
        """acct_tax_filing should be RED tier."""
        spec = get_template_spec("acct_tax_filing")
        assert spec is not None
        assert spec["risk_tier"] == "red"

    def test_every_template_has_required_fields(self) -> None:
        """Every template must have required_fields and required_fields_delta."""
        for key, spec in _TEMPLATE_REGISTRY.items():
            assert "required_fields" in spec, f"Template {key} missing required_fields"
            assert "required_fields_delta" in spec, f"Template {key} missing required_fields_delta"
            assert isinstance(spec["required_fields_delta"], list), (
                f"Template {key} required_fields_delta must be a list"
            )


# ===========================================================================
# Parametrized Template Generation Tests (all 14)
# ===========================================================================

# Build list of (template_key, needs_jurisdiction) pairs
_TEMPLATE_PARAMS = [
    (k, v.get("jurisdiction_required", False))
    for k, v in _TEMPLATE_REGISTRY.items()
]


class TestGenerateAllTemplates:
    """Verify generate_contract accepts all 14 real template types."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("template_key,needs_jurisdiction", _TEMPLATE_PARAMS)
    async def test_generate_accepts_template(
        self, clara: ClaraLegalSkillPack, template_key: str, needs_jurisdiction: bool
    ) -> None:
        """generate_contract should accept every registered template type."""
        ctx = _ctx()
        spec = get_template_spec(template_key)
        assert spec is not None

        # Build terms with all required_fields_delta + jurisdiction if needed
        terms: dict = {"title": f"Test {template_key}"}
        if needs_jurisdiction:
            terms["jurisdiction_state"] = "NY"
        for field_name in spec.get("required_fields_delta", []):
            if field_name not in terms:
                terms[field_name] = f"test-{field_name}"

        result = await clara.generate_contract(
            template_type=template_key,
            parties=[{"name": "Test Party", "email": "test@example.com", "role": "signer"}],
            terms=terms,
            context=ctx,
        )

        assert result.success is True, (
            f"Template '{template_key}' should succeed but got error: {result.error}"
        )
        assert result.receipt["event_type"] == "contract.generate"
        assert result.receipt["suite_id"] == ctx.suite_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alias,resolved", [
        ("nda", "general_mutual_nda"),
        ("sow", "trades_sow"),
        ("hvac", "trades_hvac_proposal"),
        ("sublease", "landlord_commercial_sublease"),
        ("w9", "general_w9"),
    ])
    async def test_legacy_alias_generates(
        self, clara: ClaraLegalSkillPack, alias: str, resolved: str
    ) -> None:
        """Legacy aliases pointing to real templates should resolve and generate successfully."""
        ctx = _ctx()
        spec = get_template_spec(alias)
        terms: dict = {"title": f"Test {alias}"}
        if spec and spec.get("jurisdiction_required"):
            terms["jurisdiction_state"] = "CA"
        for field_name in (spec or {}).get("required_fields_delta", []):
            if field_name not in terms:
                terms[field_name] = f"test-{field_name}"

        result = await clara.generate_contract(
            template_type=alias,
            parties=[{"name": "Alias Party", "email": "alias@test.com", "role": "signer"}],
            terms=terms,
            context=ctx,
        )

        assert result.success is True, f"Alias '{alias}' failed: {result.error}"
        assert result.data["template_type"] == resolved


# ===========================================================================
# Jurisdiction Enforcement Tests
# ===========================================================================

class TestJurisdictionEnforcement:
    """Templates with jurisdiction_required=true must fail without jurisdiction_state."""

    @pytest.mark.asyncio
    async def test_trades_sow_requires_jurisdiction(self, clara: ClaraLegalSkillPack) -> None:
        """trades_sow requires jurisdiction_state."""
        ctx = _ctx()
        result = await clara.generate_contract(
            template_type="trades_sow",
            parties=[{"name": "Test", "email": "t@t.com", "role": "signer"}],
            terms={"title": "Test SoW", "milestones": "M1", "pricing": "$100"},
            context=ctx,
        )
        assert result.success is False
        assert "jurisdiction_state" in (result.error or "")
        assert result.receipt["policy"]["reasons"] == ["MISSING_JURISDICTION"]

    @pytest.mark.asyncio
    async def test_nda_requires_jurisdiction(self, clara: ClaraLegalSkillPack) -> None:
        """general_mutual_nda requires jurisdiction_state."""
        ctx = _ctx()
        result = await clara.generate_contract(
            template_type="general_mutual_nda",
            parties=[{"name": "Test", "email": "t@t.com", "role": "signer"}],
            terms={"title": "NDA", "purpose": "x", "term_length": "1y"},
            context=ctx,
        )
        assert result.success is False
        assert result.receipt["policy"]["reasons"] == ["MISSING_JURISDICTION"]

    @pytest.mark.asyncio
    async def test_painting_proposal_no_jurisdiction_required(self, clara: ClaraLegalSkillPack) -> None:
        """trades_painting_proposal does NOT require jurisdiction."""
        ctx = _ctx()
        result = await clara.generate_contract(
            template_type="trades_painting_proposal",
            parties=[{"name": "Test", "email": "t@t.com", "role": "signer"}],
            terms={"title": "Painting Proposal", "scope_description": "Exterior painting"},
            context=ctx,
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_commercial_sublease_red_tier_with_jurisdiction(self, clara: ClaraLegalSkillPack) -> None:
        """landlord_commercial_sublease is RED tier and requires jurisdiction."""
        ctx = _ctx()
        result = await clara.generate_contract(
            template_type="landlord_commercial_sublease",
            parties=[{"name": "Landlord LLC", "email": "l@t.com", "role": "landlord"}],
            terms={
                "title": "Commercial Sublease",
                "jurisdiction_state": "NY",
                "property_address": "123 Main St",
                "lease_term": "12 months",
                "monthly_rent": "1500",
            },
            context=ctx,
        )
        assert result.success is True
        assert result.data["risk_tier"] == "red"
        assert result.presence_required is True

    @pytest.mark.asyncio
    async def test_evil_jurisdiction_whitespace_bypass(self, clara: ClaraLegalSkillPack) -> None:
        """EVIL: Whitespace-only jurisdiction_state must be rejected (Law #3)."""
        ctx = _ctx()
        result = await clara.generate_contract(
            template_type="trades_sow",
            parties=[{"name": "Test", "email": "t@t.com", "role": "signer"}],
            terms={"title": "SoW", "milestones": "M1", "pricing": "$100",
                   "jurisdiction_state": "   "},  # whitespace-only bypass attempt
            context=ctx,
        )
        assert result.success is False
        assert result.receipt["policy"]["reasons"] == ["MISSING_JURISDICTION"]

    @pytest.mark.asyncio
    async def test_evil_jurisdiction_empty_string_bypass(self, clara: ClaraLegalSkillPack) -> None:
        """EVIL: Empty-string jurisdiction_state must be rejected."""
        ctx = _ctx()
        result = await clara.generate_contract(
            template_type="general_mutual_nda",
            parties=[{"name": "Test", "email": "t@t.com", "role": "signer"}],
            terms={"title": "NDA", "purpose": "x", "term_length": "1y",
                   "jurisdiction_state": ""},
            context=ctx,
        )
        assert result.success is False
        assert result.receipt["policy"]["reasons"] == ["MISSING_JURISDICTION"]


# ===========================================================================
# Preflight Validation Tests
# ===========================================================================

class TestPreflightValidation:
    """Test preflight_validate catches missing required_fields_delta."""

    def test_mutual_nda_missing_purpose(self) -> None:
        """Mutual NDA missing 'purpose' should fail preflight."""
        errors = preflight_validate("general_mutual_nda", {"term_length": "2y"})
        assert any("purpose" in e for e in errors)

    def test_mutual_nda_valid(self) -> None:
        """Mutual NDA with all fields should pass."""
        errors = preflight_validate("general_mutual_nda", {
            "purpose": "Partnership exploration",
            "term_length": "2 years",
            "jurisdiction_state": "NY",
        })
        assert errors == []

    def test_trades_sow_missing_milestones(self) -> None:
        """trades_sow missing milestones should fail."""
        errors = preflight_validate("trades_sow", {"pricing": "fixed"})
        assert any("milestones" in e for e in errors)

    def test_unknown_template(self) -> None:
        """Unknown template should return error."""
        errors = preflight_validate("nonexistent", {})
        assert any("Unknown template" in e for e in errors)

    def test_commercial_sublease_requires_fields(self) -> None:
        """landlord_commercial_sublease has 3 required delta fields."""
        errors = preflight_validate("landlord_commercial_sublease", {})
        # property_address, lease_term, monthly_rent
        assert len(errors) >= 3


# ===========================================================================
# Webhook Handler Tests
# ===========================================================================

class TestWebhookHandler:
    """Test PandaDoc webhook handler (HMAC, idempotency, state mapping)."""

    def _sign_event(self, event: dict, secret: str = "test-secret-123") -> tuple[bytes, str]:
        """Helper: serialize event and compute HMAC signature."""
        raw = json.dumps(event).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return raw, sig

    def test_process_valid_event(self, webhook_handler: PandaDocWebhookHandler) -> None:
        """Valid webhook event with valid HMAC should be processed and receipt returned."""
        event = {
            "event_id": "evt-001",
            "event": "document_state_change",
            "data": {
                "id": "doc-abc",
                "status": "document.completed",
                "metadata": {
                    "aspire_suite_id": "suite-001",
                    "aspire_office_id": "office-001",
                    "aspire_correlation_id": "corr-001",
                },
            },
        }
        raw_body, signature = self._sign_event(event)
        receipt = webhook_handler.process_event(event, raw_body=raw_body, signature=signature)
        assert receipt["status"] == "ok"
        assert receipt["metadata"]["event_id"] == "evt-001"
        assert receipt["metadata"]["document_id"] == "doc-abc"

    def test_duplicate_event_rejected(self, webhook_handler: PandaDocWebhookHandler) -> None:
        """Duplicate event_id should raise WebhookDuplicateError."""
        event = {
            "event_id": "evt-dup",
            "event": "document_state_change",
            "data": {"id": "doc-dup", "status": "document.sent", "metadata": {}},
        }
        raw_body, signature = self._sign_event(event)
        webhook_handler.process_event(event, raw_body=raw_body, signature=signature)  # first time

        with pytest.raises(WebhookDuplicateError) as exc_info:
            webhook_handler.process_event(event, raw_body=raw_body, signature=signature)  # duplicate
        # Law #2: denial must carry receipt
        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt["policy"]["decision"] == "deny"
        assert exc_info.value.receipt["policy"]["reasons"] == ["DUPLICATE_EVENT_ID"]

    def test_missing_signature_fails_closed(self, webhook_handler: PandaDocWebhookHandler) -> None:
        """Missing raw_body/signature must fail closed with receipt (Law #2 + #3)."""
        event = {"event_id": "evt-no-sig", "event": "test", "data": {"id": "d", "status": "s", "metadata": {}}}
        with pytest.raises(WebhookSignatureError, match="Missing raw body") as exc_info:
            webhook_handler.process_event(event)  # no raw_body/signature
        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt["policy"]["reasons"] == ["MISSING_SIGNATURE"]

    def test_missing_secret_fails_closed(self) -> None:
        """Empty webhook secret must fail closed with receipt (Law #2 + #3)."""
        handler = PandaDocWebhookHandler(webhook_secret="")
        event = {"event_id": "evt-no-secret", "event": "test", "data": {"id": "d", "status": "s", "metadata": {}}}
        with pytest.raises(WebhookSignatureError, match="not configured") as exc_info:
            handler.process_event(event, raw_body=b"body", signature="sig")
        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt["policy"]["reasons"] == ["WEBHOOK_SECRET_NOT_CONFIGURED"]

    def test_whitespace_secret_fails_closed(self) -> None:
        """Whitespace-only webhook secret must fail closed (not bypass HMAC)."""
        handler = PandaDocWebhookHandler(webhook_secret="   ")
        event = {"event_id": "evt-ws", "event": "test", "data": {"id": "d", "status": "s", "metadata": {}}}
        with pytest.raises(WebhookSignatureError, match="not configured") as exc_info:
            handler.process_event(event, raw_body=b"body", signature="sig")
        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt["policy"]["reasons"] == ["WEBHOOK_SECRET_NOT_CONFIGURED"]

    def test_set_webhook_secret_rejects_empty(self) -> None:
        """set_webhook_secret must reject empty/whitespace strings."""
        handler = PandaDocWebhookHandler(webhook_secret="valid")
        with pytest.raises(ValueError, match="cannot be empty"):
            handler.set_webhook_secret("")
        with pytest.raises(ValueError, match="cannot be empty"):
            handler.set_webhook_secret("   ")

    def test_status_mapping(self) -> None:
        """PandaDoc statuses should map to correct state machine states."""
        assert map_pandadoc_status_to_state("document.draft") == "draft"
        assert map_pandadoc_status_to_state("document.sent") == "sent"
        assert map_pandadoc_status_to_state("document.completed") == "signed"
        assert map_pandadoc_status_to_state("document.voided") == "expired"
        assert map_pandadoc_status_to_state("document.declined") == "expired"
        assert map_pandadoc_status_to_state("document.viewed") == "sent"
        assert map_pandadoc_status_to_state("unknown_status") is None


class TestWebhookHMAC:
    """Test HMAC signature verification on webhooks."""

    def test_valid_signature_passes(self) -> None:
        """Valid HMAC signature should pass verification."""
        secret = "test-secret"
        payload = b'{"event_id": "test"}'
        sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert verify_pandadoc_signature(payload, sig, secret) is True

    def test_invalid_signature_fails(self) -> None:
        """Invalid HMAC signature should raise WebhookSignatureError."""
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_pandadoc_signature(b"payload", "bad-signature", "secret")

    def test_missing_signature_fails(self) -> None:
        """Missing signature header should raise WebhookSignatureError."""
        with pytest.raises(WebhookSignatureError, match="Missing"):
            verify_pandadoc_signature(b"payload", "", "secret")

    def test_missing_secret_fails(self) -> None:
        """Missing webhook secret should raise WebhookSignatureError."""
        with pytest.raises(WebhookSignatureError, match="not configured"):
            verify_pandadoc_signature(b"payload", "sig", "")


# ===========================================================================
# Contract Outbox Tests
# ===========================================================================

class TestContractOutbox:
    """Test contract outbox persistence and idempotency."""

    def test_persist_success(self, outbox: ContractOutbox) -> None:
        """Valid contract should be persisted with receipt."""
        record = ContractRecord(
            document_id="doc-001",
            template_key="general_mutual_nda",
            template_lane="general",
            suite_id="suite-001",
            office_id="office-001",
            correlation_id="corr-001",
            parties=[{"name": "Acme", "email": "a@a.com"}],
            title="Mutual NDA",
        )
        result = outbox.persist(record)
        assert result.success is True
        assert result.receipt["status"] == "ok"
        assert result.receipt["metadata"]["document_id"] == "doc-001"

    def test_persist_idempotent(self, outbox: ContractOutbox) -> None:
        """Same document_id + suite_id should be idempotent."""
        record = ContractRecord(
            document_id="doc-idem",
            template_key="trades_sow",
            template_lane="trades",
            suite_id="suite-001",
            office_id="office-001",
            correlation_id="corr-001",
            parties=[],
        )
        result1 = outbox.persist(record)
        result2 = outbox.persist(record)
        assert result1.success is True
        assert result2.success is True
        assert result2.receipt["metadata"].get("already_persisted") is True

    def test_persist_missing_document_id(self, outbox: ContractOutbox) -> None:
        """Missing document_id should fail closed (Law #3)."""
        record = ContractRecord(
            document_id="",
            template_key="nda",
            template_lane="general",
            suite_id="suite-001",
            office_id="office-001",
            correlation_id="corr-001",
            parties=[],
        )
        result = outbox.persist(record)
        assert result.success is False
        assert "MISSING_DOCUMENT_ID" in result.receipt["policy"]["reasons"]

    def test_persist_missing_suite_id(self, outbox: ContractOutbox) -> None:
        """Missing suite_id should fail closed (Law #6)."""
        record = ContractRecord(
            document_id="doc-no-suite",
            template_key="nda",
            template_lane="general",
            suite_id="",
            office_id="office-001",
            correlation_id="corr-001",
            parties=[],
        )
        result = outbox.persist(record)
        assert result.success is False
        assert "MISSING_SUITE_ID" in result.receipt["policy"]["reasons"]

    def test_tenant_isolation_on_get(self, outbox: ContractOutbox) -> None:
        """get_contract should respect suite_id scoping (Law #6)."""
        record = ContractRecord(
            document_id="doc-tenant-a",
            template_key="nda",
            template_lane="general",
            suite_id="suite-A",
            office_id="office-A",
            correlation_id="corr-001",
            parties=[],
        )
        outbox.persist(record)

        # Same suite can retrieve
        assert outbox.get_contract("doc-tenant-a", "suite-A") is not None
        # Different suite cannot retrieve
        assert outbox.get_contract("doc-tenant-a", "suite-B") is None

    def test_tenant_isolation_on_list(self, outbox: ContractOutbox) -> None:
        """list_contracts should only return contracts for the given suite."""
        for suite in ("suite-X", "suite-Y"):
            outbox.persist(ContractRecord(
                document_id=f"doc-{suite}",
                template_key="nda",
                template_lane="general",
                suite_id=suite,
                office_id="office-1",
                correlation_id="corr-1",
                parties=[],
            ))

        assert len(outbox.list_contracts("suite-X")) == 1
        assert len(outbox.list_contracts("suite-Y")) == 1
        assert len(outbox.list_contracts("suite-Z")) == 0

    def test_queue_for_retry(self, outbox: ContractOutbox) -> None:
        """Failed writes should be queued for retry."""
        record = ContractRecord(
            document_id="doc-retry",
            template_key="sow",
            template_lane="trades",
            suite_id="suite-001",
            office_id="office-001",
            correlation_id="corr-001",
            parties=[],
        )
        result = outbox.queue_for_retry(record)
        assert result.success is True
        assert result.queued_for_retry is True
        assert outbox.retry_queue_depth == 1


# ===========================================================================
# Contract State Machine Edge Cases
# ===========================================================================

class TestStateMachineEdgeCases:
    """Test contract state machine boundary conditions."""

    def test_double_sign_attempt(self) -> None:
        """Cannot sign a contract that's already signed (no SIGNED->SIGNED)."""
        sm = ContractStateMachine("c-1", "s-1", "o-1")
        # DRAFT -> REVIEWED
        sm.transition("c-1", "draft", "reviewed", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="user-1")
        # REVIEWED -> SENT
        sm.transition("c-1", "reviewed", "sent", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="user-1",
                      approval_evidence={"approved_by": "user-1"})
        # SENT -> SIGNED
        sm.transition("c-1", "sent", "signed", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="user-1",
                      approval_evidence={"approved_by": "user-1"},
                      presence_token="pres-1")

        # SIGNED -> SIGNED should fail
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("c-1", "signed", "signed", suite_id="s-1", office_id="o-1",
                          correlation_id="corr", actor_id="user-2",
                          approval_evidence={"approved_by": "user-2"},
                          presence_token="pres-2")

        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"

    def test_expired_then_sign_fails(self) -> None:
        """Cannot sign a contract after it expired."""
        sm = ContractStateMachine("c-2", "s-1", "o-1")
        sm.transition("c-2", "draft", "reviewed", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="user-1")
        sm.transition("c-2", "reviewed", "sent", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="user-1",
                      approval_evidence={"approved_by": "user-1"})
        # Expire it
        sm.transition("c-2", "sent", "expired", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="system",
                      approval_evidence={"reason": "timeout"})

        assert sm.is_terminal is True

        # Try to sign expired
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("c-2", "expired", "signed", suite_id="s-1", office_id="o-1",
                          correlation_id="corr", actor_id="user-1",
                          approval_evidence={"approved_by": "user-1"},
                          presence_token="pres-1")

        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"

    def test_full_happy_path(self) -> None:
        """Full lifecycle: DRAFT -> REVIEWED -> SENT -> SIGNED -> ARCHIVED."""
        sm = ContractStateMachine("c-3", "s-1", "o-1")
        assert sm.current_state == "draft"

        sm.transition("c-3", "draft", "reviewed", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="clara")
        assert sm.current_state == "reviewed"

        sm.transition("c-3", "reviewed", "sent", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="user-1",
                      approval_evidence={"approved_by": "user-1"})
        assert sm.current_state == "sent"

        sm.transition("c-3", "sent", "signed", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="signer-1",
                      approval_evidence={"signer": "Jane Doe"},
                      presence_token="pres-token-xyz")
        assert sm.current_state == "signed"

        sm.transition("c-3", "signed", "archived", suite_id="s-1", office_id="o-1",
                      correlation_id="corr", actor_id="system")
        assert sm.current_state == "archived"
        assert sm.is_terminal is True
        assert len(sm.history) == 4


# ===========================================================================
# Evil Tests — Security-Critical Attack Scenarios
# ===========================================================================

class TestEvilClara:
    """Evil tests for Clara Legal — attack scenario validation."""

    @pytest.mark.asyncio
    async def test_evil_unknown_template_denied(self, clara: ClaraLegalSkillPack) -> None:
        """EVIL: Unknown template key must be denied with receipt."""
        ctx = _ctx()
        evil_templates = [
            "../../etc/passwd",
            "admin_override",
            "nda; DROP TABLE contracts",
            "__proto__",
            "trades_sow\x00hidden",
            "",
        ]
        for evil_tpl in evil_templates:
            result = await clara.generate_contract(
                template_type=evil_tpl,
                parties=[{"name": "Evil", "email": "evil@test.com"}],
                terms={"title": "Evil"},
                context=ctx,
            )
            assert result.success is False, f"Evil template '{evil_tpl}' should be denied"
            assert result.receipt["policy"]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_evil_cross_tenant_outbox(self) -> None:
        """EVIL: Contract from suite A must not be visible to suite B."""
        outbox = ContractOutbox()
        outbox.persist(ContractRecord(
            document_id="doc-secret",
            template_key="nda",
            template_lane="general",
            suite_id="suite-victim",
            office_id="office-v",
            correlation_id="corr-1",
            parties=[{"name": "Victim Corp", "email": "v@v.com"}],
        ))

        # Attacker tries to access
        assert outbox.get_contract("doc-secret", "suite-attacker") is None
        assert len(outbox.list_contracts("suite-attacker")) == 0
        outbox.clear_store()

    def test_evil_webhook_hmac_forgery(self) -> None:
        """EVIL: Forged webhook signature must be rejected."""
        with pytest.raises(WebhookSignatureError):
            verify_pandadoc_signature(
                b'{"event":"fake_event","data":{}}',
                "forged-signature-12345",
                "real-secret",
            )

    def test_evil_state_machine_cross_tenant(self) -> None:
        """EVIL: State machine must reject transitions from wrong suite (Law #6)."""
        sm = ContractStateMachine("c-1", "suite-A", "office-A")
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("c-1", "draft", "reviewed",
                          suite_id="suite-B",  # WRONG suite
                          office_id="office-A",
                          correlation_id="corr",
                          actor_id="attacker")
        assert exc_info.value.denial_receipt.reason_code == "suite_id_mismatch"

    @pytest.mark.asyncio
    async def test_evil_red_tier_always_presence(self, clara: ClaraLegalSkillPack) -> None:
        """EVIL: landlord_commercial_sublease (RED) must ALWAYS require presence."""
        ctx = _ctx()
        result = await clara.generate_contract(
            template_type="landlord_commercial_sublease",
            parties=[{"name": "Landlord", "email": "l@l.com", "role": "landlord"}],
            terms={
                "title": "Sublease",
                "jurisdiction_state": "NY",
                "property_address": "123 Main",
                "lease_term": "12m",
                "monthly_rent": "1500",
            },
            context=ctx,
        )
        assert result.success is True
        assert result.presence_required is True, "RED tier MUST require presence"
        assert result.data["risk_tier"] == "red"


# ---------------------------------------------------------------------------
# Template Discovery Tool Tests (pandadoc_client level)
# ---------------------------------------------------------------------------


class TestPandaDocTemplateTools:
    """Tests for pandadoc.templates.list and pandadoc.templates.details tool functions."""

    @pytest.mark.asyncio
    async def test_templates_list_success(self) -> None:
        """pandadoc.templates.list returns template list from PandaDoc API."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_templates_list,
        )

        mock_body = {
            "results": [
                {"id": "tmpl-1", "name": "NDA Template", "date_created": "2026-01-01"},
                {"id": "tmpl-2", "name": "SOW Template", "date_created": "2026-01-15"},
            ]
        }

        with patch(
            "aspire_orchestrator.providers.pandadoc_client.PandaDocClient._request",
            new=AsyncMock(return_value=_mock_response(True, 200, mock_body)),
        ):
            result = await execute_pandadoc_templates_list(
                payload={"q": "NDA"},
                correlation_id="corr-1",
                suite_id="suite-1",
                office_id="office-1",
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["count"] == 2
        assert result.data["templates"][0]["name"] == "NDA Template"
        assert result.receipt_data is not None

    @pytest.mark.asyncio
    async def test_templates_list_empty(self) -> None:
        """pandadoc.templates.list returns empty list when no matches."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_templates_list,
        )

        with patch(
            "aspire_orchestrator.providers.pandadoc_client.PandaDocClient._request",
            new=AsyncMock(return_value=_mock_response(True, 200, {"results": []})),
        ):
            result = await execute_pandadoc_templates_list(
                payload={},
                correlation_id="corr-1",
                suite_id="suite-1",
                office_id="office-1",
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["count"] == 0

    @pytest.mark.asyncio
    async def test_templates_list_api_failure(self) -> None:
        """pandadoc.templates.list fails with receipt when API errors."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_templates_list,
        )

        with patch(
            "aspire_orchestrator.providers.pandadoc_client.PandaDocClient._request",
            new=AsyncMock(return_value=_mock_response(False, 401, {}, "Auth failed")),
        ):
            result = await execute_pandadoc_templates_list(
                payload={},
                correlation_id="corr-1",
                suite_id="suite-1",
                office_id="office-1",
            )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data is not None

    @pytest.mark.asyncio
    async def test_templates_details_success(self) -> None:
        """pandadoc.templates.details returns fields/tokens/roles."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_templates_details,
        )

        mock_body = {
            "name": "Mutual NDA",
            "fields": [
                {"name": "EffectiveDate", "type": "date", "merge_field": "", "assigned_to": {}},
            ],
            "tokens": [
                {"name": "Client.Name", "value": ""},
                {"name": "Client.Email", "value": ""},
            ],
            "roles": [
                {"name": "Owner", "signing_order": 1},
                {"name": "Client", "signing_order": 2},
            ],
            "images": [],
            "content_placeholders": [],
        }

        with patch(
            "aspire_orchestrator.providers.pandadoc_client.PandaDocClient._request",
            new=AsyncMock(return_value=_mock_response(True, 200, mock_body)),
        ):
            result = await execute_pandadoc_templates_details(
                payload={"template_id": "tmpl-abc-123"},
                correlation_id="corr-1",
                suite_id="suite-1",
                office_id="office-1",
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["field_count"] == 1
        assert result.data["token_count"] == 2
        assert result.data["role_count"] == 2
        assert result.data["tokens"][0]["name"] == "Client.Name"

    @pytest.mark.asyncio
    async def test_templates_details_missing_id(self) -> None:
        """pandadoc.templates.details fails without template_id (Law #3)."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_templates_details,
        )

        result = await execute_pandadoc_templates_details(
            payload={},
            correlation_id="corr-1",
            suite_id="suite-1",
            office_id="office-1",
        )

        assert result.outcome == Outcome.FAILED
        assert "template_id" in (result.error or "")
        assert result.receipt_data is not None

    @pytest.mark.asyncio
    async def test_templates_list_respects_count_limit(self) -> None:
        """pandadoc.templates.list caps count at 100 (defensive)."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_templates_list,
        )

        with patch(
            "aspire_orchestrator.providers.pandadoc_client.PandaDocClient._request",
            new=AsyncMock(return_value=_mock_response(True, 200, {"results": []})),
        ) as mock_req:
            await execute_pandadoc_templates_list(
                payload={"count": 999},  # Should be capped to 100
                correlation_id="corr-1",
                suite_id="suite-1",
                office_id="office-1",
            )

        # Verify the path includes count=100 (capped), not 999
        call_args = mock_req.call_args
        path = call_args[0][0].path
        assert "count=100" in path


class TestLiveTemplateScan:
    """Tests for Clara's multi-strategy PandaDoc template discovery.

    Verifies:
    - Multi-word search fallback to individual keywords
    - Full-list fallback when all searches return empty
    - Fuzzy matching works with LLM-transformed template_type values
    """

    @pytest.mark.asyncio
    async def test_keyword_fallback_on_empty_multiword_search(self) -> None:
        """When 'mutual nda' returns empty but 'nda' returns results, Clara finds the template."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _resolve_template_for_pandadoc,
        )

        nda_template = {"id": "tmpl-nda", "name": "NDA Template"}
        call_count = 0

        async def mock_request(self, req):
            nonlocal call_count
            call_count += 1
            q = req.query_params.get("q", "")
            # "mutual nda" returns empty, "nda" returns a result
            if "mutual" in q.lower():
                return _mock_response(True, 200, {"results": []})
            if "nda" in q.lower():
                return _mock_response(True, 200, {"results": [nda_template]})
            return _mock_response(True, 200, {"results": []})

        with patch(
            "aspire_orchestrator.providers.pandadoc_client.PandaDocClient._request",
            new=mock_request,
        ), patch(
            "aspire_orchestrator.skillpacks.clara_legal._resolve_template_key",
            return_value="",
        ), patch(
            "aspire_orchestrator.skillpacks.clara_legal.get_template_spec",
            return_value=None,
        ):
            uuid, name, err = await _resolve_template_for_pandadoc(
                {"template_type": "Mutual NDA", "parties": []}
            )

        assert uuid == "tmpl-nda", f"Expected tmpl-nda, got {uuid}"
        assert err == ""
        assert call_count >= 2  # At least tried multi-word then single keyword

    @pytest.mark.asyncio
    async def test_list_all_fallback_on_empty_search(self) -> None:
        """When all keyword searches return empty, Clara lists ALL templates and matches."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _resolve_template_for_pandadoc,
        )

        nda_template = {"id": "tmpl-nda", "name": "NDA Template"}

        async def mock_request(self, req):
            q = req.query_params.get("q", "")
            # All search queries return empty
            if q:
                return _mock_response(True, 200, {"results": []})
            # But listing all (no q param) returns templates
            return _mock_response(True, 200, {"results": [nda_template]})

        with patch(
            "aspire_orchestrator.providers.pandadoc_client.PandaDocClient._request",
            new=mock_request,
        ), patch(
            "aspire_orchestrator.skillpacks.clara_legal._resolve_template_key",
            return_value="",
        ), patch(
            "aspire_orchestrator.skillpacks.clara_legal.get_template_spec",
            return_value=None,
        ):
            uuid, name, err = await _resolve_template_for_pandadoc(
                {"template_type": "nda", "parties": []}
            )

        assert uuid == "tmpl-nda"

    def test_find_best_match_with_llm_transformed_type(self) -> None:
        """Fuzzy matcher works when template_type is LLM-transformed (e.g. 'Mutual NDA')."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _find_best_template_match,
        )

        results = [
            {"id": "tmpl-1", "name": "HVAC Contract for Services"},
            {"id": "tmpl-2", "name": "NDA Template"},
        ]
        # LLM transforms "nda" → "Mutual NDA"
        match = _find_best_template_match(results, "Mutual NDA")
        assert match is not None
        assert match["id"] == "tmpl-2"

    def test_find_best_match_with_registry_key(self) -> None:
        """Fuzzy matcher works with registry key (e.g. 'general_mutual_nda')."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _find_best_template_match,
        )

        results = [
            {"id": "tmpl-1", "name": "HVAC Contract for Services"},
            {"id": "tmpl-2", "name": "NDA Template"},
        ]
        match = _find_best_template_match(results, "general_mutual_nda")
        assert match is not None
        assert match["id"] == "tmpl-2"

    def test_build_search_terms_fallback_for_unknown_type(self) -> None:
        """_build_template_search_terms handles LLM-generated types not in SEARCH_MAP."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _build_template_search_terms,
        )

        # "Mutual NDA" is not a registry key — fallback strips underscores
        result = _build_template_search_terms("Mutual NDA")
        assert "mutual" in result.lower()
        assert "nda" in result.lower()


class TestSmartTokenMapping:
    """Tests for Clara's intelligent token mapping.

    Verifies:
    - Company names go to Company tokens, not FirstName/LastName (no doubling)
    - Suite profile enriches sender data (owner name, address)
    - Missing tokens are detected and reported
    - _is_company_name correctly distinguishes companies from people
    """

    def test_is_company_name_detects_llc(self) -> None:
        from aspire_orchestrator.providers.pandadoc_client import _is_company_name
        assert _is_company_name("Skytech Tower LLC") is True
        assert _is_company_name("BuildRight Solutions Inc") is True
        assert _is_company_name("Acme Corp") is True
        assert _is_company_name("Smith & Partners") is True

    def test_is_company_name_rejects_person(self) -> None:
        from aspire_orchestrator.providers.pandadoc_client import _is_company_name
        assert _is_company_name("Antonio Towers") is False
        assert _is_company_name("John Smith") is False
        assert _is_company_name("Jane") is False

    def test_split_person_name(self) -> None:
        from aspire_orchestrator.providers.pandadoc_client import _split_person_name
        assert _split_person_name("Antonio Towers") == ("Antonio", "Towers")
        assert _split_person_name("John Michael Smith") == ("John", "Michael Smith")
        assert _split_person_name("Madonna") == ("Madonna", "")
        assert _split_person_name("") == ("", "")

    @pytest.mark.asyncio
    async def test_token_builder_uses_profile_not_company_split(self) -> None:
        """Owner name from profile goes to FirstName/LastName, company name stays in Company."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _fetch_template_details_and_build_tokens,
            PandaDocClient,
        )

        mock_profile = {
            "name": "Antonio Towers",
            "owner_name": "Antonio Towers",
            "email": "antonio@skytech.com",
            "business_name": "Skytech Tower LLC",
            "business_address_line1": "123 Main St",
            "business_city": "Miami",
            "business_state": "FL",
            "business_zip": "33101",
            "business_address_same_as_home": False,
        }

        template_details_body = {
            "tokens": [
                {"name": "Sender.Company"},
                {"name": "Sender.FirstName"},
                {"name": "Sender.LastName"},
                {"name": "Sender.State"},
                {"name": "Sender.Address"},
                {"name": "Client.Company"},
                {"name": "Client.FirstName"},
                {"name": "Client.LastName"},
                {"name": "Document.CreatedDate"},
            ],
            "roles": [{"name": "Sender"}, {"name": "Client"}],
        }

        payload = {
            "parties": [
                {"name": "Skytech Tower LLC"},
                {"name": "BuildRight Solutions Inc"},
            ],
            "terms": {"jurisdiction_state": "FL"},
        }

        mock_client = MagicMock(spec=PandaDocClient)
        mock_client._request = AsyncMock(
            return_value=_mock_response(True, 200, template_details_body),
        )

        with patch(
            "aspire_orchestrator.providers.pandadoc_client._fetch_suite_profile",
            return_value=mock_profile,
        ):
            tokens, roles, missing, _fields, _content_ph = await _fetch_template_details_and_build_tokens(
                mock_client, "tmpl-123", payload, suite_id="suite-123",
            )

        token_map = {t["name"]: t["value"] for t in tokens}

        # Company name goes to Company token — NOT duplicated into FirstName/LastName
        assert token_map["Sender.Company"] == "Skytech Tower LLC"
        # Owner name from profile goes to FirstName/LastName
        assert token_map["Sender.FirstName"] == "Antonio"
        assert token_map["Sender.LastName"] == "Towers"
        # Address from profile
        assert "123 Main St" in token_map["Sender.Address"]
        assert "Miami" in token_map["Sender.Address"]
        assert token_map["Sender.State"] == "FL"
        # Client company stays as company
        assert token_map["Client.Company"] == "BuildRight Solutions Inc"
        # Client person names are empty (not filled from company split)
        assert token_map["Client.FirstName"] == ""
        assert token_map["Client.LastName"] == ""
        # Missing tokens include client person fields
        assert "Client.FirstName" in missing
        assert "Client.LastName" in missing
        # Sender fields should NOT be in missing
        assert "Sender.Company" not in missing
        assert "Sender.FirstName" not in missing

    @pytest.mark.asyncio
    async def test_token_builder_graceful_without_profile(self) -> None:
        """When suite profile is unavailable, tokens are built from payload only."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _fetch_template_details_and_build_tokens,
            PandaDocClient,
        )

        template_details_body = {
            "tokens": [
                {"name": "Sender.Company"},
                {"name": "Client.Company"},
                {"name": "Document.CreatedDate"},
            ],
            "roles": [],
        }

        payload = {
            "parties": [{"name": "Skytech Tower LLC"}, {"name": "BuildRight Inc"}],
            "terms": {},
        }

        mock_client = MagicMock(spec=PandaDocClient)
        mock_client._request = AsyncMock(
            return_value=_mock_response(True, 200, template_details_body),
        )

        with patch(
            "aspire_orchestrator.providers.pandadoc_client._fetch_suite_profile",
            return_value={},
        ):
            tokens, roles, missing, _fields, _content_ph = await _fetch_template_details_and_build_tokens(
                mock_client, "tmpl-123", payload, suite_id="suite-123",
            )

        token_map = {t["name"]: t["value"] for t in tokens}
        # Company names from payload parties
        assert token_map["Sender.Company"] == "Skytech Tower LLC"
        assert token_map["Client.Company"] == "BuildRight Inc"
        # Date always filled
        assert token_map["Document.CreatedDate"] != ""

    def test_missing_token_questions_grouped(self) -> None:
        """Missing token questions are grouped into natural conversations."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _build_missing_token_questions,
        )

        questions = _build_missing_token_questions([
            "Client.FirstName", "Client.LastName", "Client.Email",
            "Sender.Address", "Sender.Phone",
        ])

        # Should group client person into one question
        assert any("contact person" in q.lower() or "signer" in q.lower() for q in questions)
        # Should group sender business into one question mentioning profile
        assert any("profile" in q.lower() or "business" in q.lower() for q in questions)
        # Should ask about client email
        assert any("email" in q.lower() for q in questions)

    def test_humanize_token_names(self) -> None:
        from aspire_orchestrator.providers.pandadoc_client import _humanize_token_name
        assert "company name" in _humanize_token_name("Sender.Company")
        assert "first name" in _humanize_token_name("Client.FirstName")
        assert "address" in _humanize_token_name("Sender.Address")

    def test_recipients_use_profile_owner_not_company_split(self) -> None:
        """Sender recipient uses owner_name from profile, not company name split."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _build_recipients_from_parties,
        )

        parties = [
            {"name": "Skytech Tower LLC", "email": "info@skytech.com"},
            {"name": "BuildRight Solutions Inc", "email": "info@buildright.com"},
        ]
        profile = {"owner_name": "Antonio Towers", "email": "antonio@skytech.com"}
        roles = [{"name": "Sender"}, {"name": "Client"}]

        recipients = _build_recipients_from_parties(parties, template_roles=roles, suite_profile=profile)

        # Sender should have owner's person name, not "Skytech" / "Tower LLC"
        assert recipients[0]["first_name"] == "Antonio"
        assert recipients[0]["last_name"] == "Towers"
        assert recipients[0]["email"] == "antonio@skytech.com"
        assert recipients[0]["role"] == "Sender"
        # Client uses company name as-is (not split into first/last)
        assert recipients[1]["first_name"] == "BuildRight Solutions Inc"
        assert recipients[1]["last_name"] == ""
        assert recipients[1]["role"] == "Client"


# ===========================================================================
# Preflight Completeness Gate Tests
# ===========================================================================

class TestPreflightCompletenessGate:
    """Tests for the preflight gate that blocks document creation when
    critical tokens are missing.

    PandaDoc tokens are ONE-WAY merge variables — they CANNOT be updated
    after document creation. So Clara must refuse to create half-blank
    documents and instead ask Ava to collect info from the user first.
    """

    @pytest.mark.asyncio
    async def test_gate_blocks_when_critical_tokens_missing(self) -> None:
        """Preflight gate blocks creation when 3+ critical tokens are empty."""
        from aspire_orchestrator.providers.pandadoc_client import (
            _fetch_template_details_and_build_tokens,
            execute_pandadoc_contract_generate,
            PandaDocClient,
        )

        # Template with many tokens, no profile, minimal payload → low fill rate
        template_details_body = {
            "tokens": [
                {"name": "Sender.Company"},
                {"name": "Sender.FirstName"},
                {"name": "Sender.LastName"},
                {"name": "Sender.Email"},
                {"name": "Sender.Address"},
                {"name": "Client.Company"},
                {"name": "Client.FirstName"},
                {"name": "Client.LastName"},
                {"name": "Client.Email"},
                {"name": "Document.CreatedDate"},
            ],
            "roles": [{"name": "Sender"}, {"name": "Client"}],
        }

        # Only party names, no profile, no contact details → many critical missing
        payload = {
            "template_type": "general_mutual_nda",
            "name": "Test NDA",
            "parties": [
                {"name": "Skytech Tower LLC"},
                {"name": "BuildRight Solutions Inc"},
            ],
            "terms": {"jurisdiction_state": "FL"},
        }

        mock_client = MagicMock(spec=PandaDocClient)
        mock_client._request = AsyncMock(
            return_value=_mock_response(True, 200, template_details_body),
        )
        mock_client.suite_limiter = MagicMock()
        mock_client.suite_limiter.acquire.return_value = True
        mock_client.rate_limiter = MagicMock()
        mock_client.rate_limiter.acquire.return_value = True
        mock_client.dedup = MagicMock()
        mock_client.dedup.compute_key.return_value = "key-1"
        mock_client.dedup.check_and_mark.return_value = False
        mock_client.make_receipt_data = MagicMock(return_value={"receipt": True})

        with (
            patch("aspire_orchestrator.providers.pandadoc_client._get_client", return_value=mock_client),
            patch("aspire_orchestrator.providers.pandadoc_client._resolve_template_for_pandadoc",
                  return_value=("tmpl-uuid-123", "Test NDA", None)),
            patch("aspire_orchestrator.providers.pandadoc_client._fetch_suite_profile",
                  return_value={}),
        ):
            result = await execute_pandadoc_contract_generate(
                payload=payload,
                correlation_id="corr-123",
                suite_id="suite-001",
                office_id="office-001",
            )

        # Gate should block — return FAILED with needs_info
        from aspire_orchestrator.models import Outcome
        assert result.outcome == Outcome.FAILED
        assert result.error == "needs_info"
        assert result.data["needs_info"] is True
        assert len(result.data["critical_missing"]) >= 3
        assert result.data["suggested_questions"]
        assert result.data["message_for_ava"]

    @pytest.mark.asyncio
    async def test_gate_allows_when_profile_fills_tokens(self) -> None:
        """Preflight gate allows creation when profile provides enough data."""
        from aspire_orchestrator.providers.pandadoc_client import (
            execute_pandadoc_contract_generate,
            PandaDocClient,
        )

        template_details_body = {
            "tokens": [
                {"name": "Sender.Company"},
                {"name": "Sender.FirstName"},
                {"name": "Sender.LastName"},
                {"name": "Client.Company"},
                {"name": "Document.CreatedDate"},
            ],
            "roles": [],
        }

        mock_profile = {
            "owner_name": "Antonio Towers",
            "business_name": "Skytech Tower LLC",
            "email": "antonio@skytech.com",
        }

        payload = {
            "template_type": "general_mutual_nda",
            "name": "Test NDA",
            "parties": [
                {
                    "role": "Sender",
                    "name": "Antonio Towers",
                    "full_name": "Antonio Towers",
                    "company": "Skytech Tower LLC",
                    "address": "123 Main St",
                    "city": "Dallas",
                    "state": "TX",
                    "zip": "75201",
                    "email": "antonio@skytech.com",
                },
                {
                    "role": "Client",
                    "name": "BuildRight Solutions Inc",
                    "full_name": "Jane Builder",
                    "company": "BuildRight Solutions Inc",
                    "address": "456 Oak Ave",
                    "city": "Austin",
                    "state": "TX",
                    "zip": "78701",
                    "email": "jane@buildright.com",
                },
            ],
            "terms": {},
        }

        # Mock the template details fetch
        mock_client = MagicMock(spec=PandaDocClient)

        # First call = template details, second call = document creation
        mock_client._request = AsyncMock(
            side_effect=[
                _mock_response(True, 200, template_details_body),  # template details
                _mock_response(True, 200, {"id": "doc-123", "name": "Test NDA", "status": "document.uploaded"}),  # create
            ],
        )
        mock_client.suite_limiter = MagicMock()
        mock_client.suite_limiter.acquire.return_value = True
        mock_client.rate_limiter = MagicMock()
        mock_client.rate_limiter.acquire.return_value = True
        mock_client.dedup = MagicMock()
        mock_client.dedup.compute_key.return_value = "key-2"
        mock_client.dedup.check_and_mark.return_value = False
        mock_client.make_receipt_data = MagicMock(return_value={"receipt": True})

        with (
            patch("aspire_orchestrator.providers.pandadoc_client._get_client", return_value=mock_client),
            patch("aspire_orchestrator.providers.pandadoc_client._resolve_template_for_pandadoc",
                  return_value=("tmpl-uuid-123", "Test NDA", None)),
            patch("aspire_orchestrator.providers.pandadoc_client._fetch_suite_profile",
                  return_value=mock_profile),
        ):
            result = await execute_pandadoc_contract_generate(
                payload=payload,
                correlation_id="corr-124",
                suite_id="suite-001",
                office_id="office-001",
            )

        # Gate should allow — all PandaDoc tokens filled + registry party data provided
        from aspire_orchestrator.models import Outcome
        assert result.outcome == Outcome.SUCCESS
        assert result.data.get("document_id") == "doc-123"


# ===========================================================================
# Narration Needs-Info Tests
# ===========================================================================

class TestNarrationNeedsInfo:
    """Tests for the narration layer's needs_info outcome path.

    When Clara's preflight gate blocks document creation, Ava needs
    a natural-sounding narration to ask the user for missing info.
    """

    def test_needs_info_with_questions(self) -> None:
        """Narration produces natural question text for needs_info outcome."""
        from aspire_orchestrator.services.narration import compose_narration

        result = compose_narration(
            outcome="needs_info",
            task_type="contract",
            tool_used="pandadoc.contract.generate",
            execution_params={"template_type": "general_mutual_nda"},
            execution_result={
                "needs_info": True,
                "template_name": "a mutual NDA",
                "missing_tokens": ["Client.FirstName", "Client.Email"],
                "suggested_questions": [
                    "Who is the contact person at the other company?",
                    "What is the other party's email address?",
                ],
            },
            draft_id=None,
            risk_tier="yellow",
        )

        assert "template" in result.lower() or "nda" in result.lower()
        assert "contact person" in result.lower() or "email" in result.lower()
        # Should NOT say "drafted" or "created" — document was NOT created
        assert "drafted" not in result.lower()
        assert "created" not in result.lower()

    def test_needs_info_with_missing_tokens_no_questions(self) -> None:
        """Narration handles missing tokens when no questions are provided."""
        from aspire_orchestrator.services.narration import compose_narration

        result = compose_narration(
            outcome="needs_info",
            task_type="contract",
            tool_used="pandadoc.contract.generate",
            execution_params={},
            execution_result={
                "needs_info": True,
                "missing_tokens": ["Sender.Address", "Client.Company"],
            },
            draft_id=None,
            risk_tier="yellow",
        )

        # Should mention profile or provide info
        assert "profile" in result.lower() or "details" in result.lower()

    def test_needs_info_fallback_no_data(self) -> None:
        """Narration provides generic fallback when no token data available."""
        from aspire_orchestrator.services.narration import compose_narration

        result = compose_narration(
            outcome="needs_info",
            task_type="contract",
            tool_used="pandadoc.contract.generate",
            execution_params={},
            execution_result={},
            draft_id=None,
            risk_tier="yellow",
        )

        assert "template" in result.lower() or "details" in result.lower()


# ===========================================================================
# Output Guard Document Quality Tests
# ===========================================================================

class TestOutputGuardDocumentQuality:
    """Tests for the output guard's document quality awareness.

    When Clara creates a document with low fill rate, the output guard
    should add a quality warning to the user-facing text.
    """

    def test_guard_adds_quality_warning_for_low_fill_rate(self) -> None:
        from aspire_orchestrator.services.output_guard import guard_output

        text = "I've drafted the NDA."
        tool_results = [
            {
                "tool_result": {
                    "needs_additional_info": True,
                    "token_quality": {
                        "fill_rate_pct": 45.0,
                        "missing_tokens": ["Client.FirstName", "Client.Email", "Client.Address"],
                    },
                },
            },
        ]

        result = guard_output(
            text=text,
            receipts=[{"outcome": "success"}],
            outcome="success",
            tool_results=tool_results,
        )

        assert "blank field" in result.lower() or "45%" in result

    def test_guard_no_warning_for_high_fill_rate(self) -> None:
        from aspire_orchestrator.services.output_guard import guard_output

        text = "I've drafted the NDA."
        tool_results = [
            {
                "tool_result": {
                    "token_quality": {
                        "fill_rate_pct": 90.0,
                        "missing_tokens": [],
                    },
                },
            },
        ]

        result = guard_output(
            text=text,
            receipts=[{"outcome": "success"}],
            outcome="success",
            tool_results=tool_results,
        )

        # No quality warning for high fill rate
        assert "blank field" not in result.lower()

    def test_guard_handles_needs_info_from_preflight(self) -> None:
        from aspire_orchestrator.services.output_guard import guard_output

        text = "Clara is working on it."
        tool_results = [
            {
                "tool_result": {
                    "needs_info": True,
                    "message_for_ava": "I need your address and the client's email before creating this document.",
                },
            },
        ]

        result = guard_output(
            text=text,
            receipts=[],
            outcome="needs_info",
            tool_results=tool_results,
        )

        assert "address" in result.lower() or "email" in result.lower()


# ===========================================================================
# Clara Skill Pack Template Hints Tests
# ===========================================================================

class TestClaraTemplateHints:
    """Tests for Clara's template field hints in the generate plan.

    Clara should tell the brain what fields the template needs
    BEFORE execution, so Ava can proactively ask the user.
    """

    @pytest.mark.asyncio
    async def test_generate_plan_includes_missing_delta_fields(self) -> None:
        """Plan includes fields_still_needed when some delta fields are provided."""
        clara = ClaraLegalSkillPack()
        ctx = _ctx()

        # Provide the required delta fields so preflight passes.
        # Use trades_sow which requires milestones + pricing.
        result = await clara.generate_contract(
            template_type="trades_sow",
            parties=[
                {"name": "Skytech Tower LLC", "email": "info@skytech.com"},
                {"name": "BuildRight Inc", "email": "info@buildright.com"},
            ],
            terms={
                "jurisdiction_state": "FL",
                "milestones": "Phase 1: Design",
                "pricing": "$50,000",
            },
            context=ctx,
        )

        assert result.success is True
        assert result.approval_required is True
        plan = result.data
        # Plan should always include the template's required fields list
        assert "template_required_fields" in plan
        assert "milestones" in plan["template_required_fields"]
        assert "pricing" in plan["template_required_fields"]

    @pytest.mark.asyncio
    async def test_generate_plan_no_hints_when_terms_complete(self) -> None:
        """Plan omits fields_still_needed when all delta fields are provided."""
        clara = ClaraLegalSkillPack()
        ctx = _ctx()

        result = await clara.generate_contract(
            template_type="trades_sow",
            parties=[
                {"name": "Skytech Tower LLC", "email": "info@skytech.com"},
                {"name": "BuildRight Inc", "email": "info@buildright.com"},
            ],
            terms={
                "jurisdiction_state": "FL",
                "milestones": "Phase 1: Design",
                "pricing": "$50,000",
            },
            context=ctx,
        )

        assert result.success is True
        plan = result.data
        assert "template_required_fields" in plan
        # All fields provided → no "fields_still_needed"
        assert "fields_still_needed" not in plan
        assert "message_for_brain" not in plan
