"""Cycle 7 Evil Tests — Skillpacks Layer (2026-03-23).

Covers gaps discovered in Cycle 7 static analysis of skillpacks/:
  BUG-SP7-01 CRITICAL: mail_ops_desk.py wrapper mail_account_create passes wrong params
  BUG-SP7-02 CRITICAL: mail_ops_desk.py wrapper domain_dns_create drops name/ttl params
  BUG-SP7-03 CRITICAL: mail_ops_desk.py wrapper domain_purchase passes wrong shape
  BUG-SP7-04 CRITICAL: teressa_books.py books_categorize passes dict instead of str
  BUG-SP7-05 CRITICAL: clara_legal.py contract_sign passes flat signer args vs dict
  BUG-SP7-06 HIGH:     Systematic LAW #2 violation — 8 rule-based packs never call store_receipts()
  BUG-SP7-07 HIGH:     EnhancedMiloPayroll.initiate_dual_approval is sync, returns dict, no receipt
  BUG-SP7-08 HIGH:     EnhancedClaraLegal.initiate_dual_approval is sync, returns dict, no receipt
  BUG-SP7-09 MEDIUM:   AvaUserSkillPack is a dead empty class (no methods)
  BUG-SP7-10 MEDIUM:   callable type annotations in adam_research.py (deprecated)
  BUG-SP7-11 MEDIUM:   teressa_books.py books_sync always passes empty date_range dict
  BUG-SP7-12 LOW:      Dead code: _handle_read_action never called in scaffold packs
  BUG-SP7-13 LOW:      Unused imports: RiskTier in tec_documents.py, nora_conference.py

Law coverage:
  Law #2: Receipt for ALL actions — every state change must produce a persisted receipt
  Law #3: Fail Closed — wrong parameters must be caught before execution, not silently succeed
  Law #4: Risk Tiers — sync non-async methods on RED-tier operations are governance gaps
  Law #6: Tenant Isolation — receipts must be scoped to the correct suite_id/office_id

CURRENT STATUS: Tests marked FAILING document bugs. Tests marked PASSING confirm safe behavior.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# Helpers
# =============================================================================

def _make_clara_context(suite_id: str = "suite-test") -> Any:
    """Build a minimal ClaraContext-like object."""
    ctx = MagicMock()
    ctx.suite_id = suite_id
    ctx.office_id = "office-test"
    ctx.correlation_id = str(uuid.uuid4())
    ctx.capability_token_id = str(uuid.uuid4())
    ctx.capability_token_hash = "abc123"
    return ctx


def _make_agent_context(suite_id: str = "suite-test") -> Any:
    """Build a minimal AgentContext-like object."""
    ctx = MagicMock()
    ctx.suite_id = suite_id
    ctx.office_id = "office-test"
    ctx.correlation_id = str(uuid.uuid4())
    return ctx


# =============================================================================
# BUG-SP7-01 — CRITICAL
# mail_ops_desk.py: mail_account_create wrapper passes wrong params to impl
# =============================================================================

class TestMailOpsMailAccountCreateSignatureMismatch:
    """Law #3: Fail Closed — wrapper must not silently pass wrong parameters.

    BUG: mail_account_create(domain_name, mailbox_name, password, context) calls
    create_mail_account(domain_name, mailbox_name, password, context) but the
    implementation at line 466 only accepts (domain_name, email_address, context).
    The 'password' arg is completely ignored and 'mailbox_name' maps to
    email_address — wrong semantic. Execution proceeds with wrong data.

    Expected: wrapper validates parameters and either maps them correctly or fails
    Actual: wrong params passed silently, password dropped from call
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/mail_ops_desk.py
    Lines: 197-204 (wrapper), 466 (implementation)
    """

    def test_mail_account_create_wrapper_passes_password_to_implementation(self):
        """FAILING: mail_account_create wrapper passes 4 args but impl only takes 3.

        The implementation create_mail_account signature is:
          async def create_mail_account(self, domain_name, email_address, context)

        The wrapper calls:
          return await self.create_mail_account(domain_name, mailbox_name, password, context)

        This passes `password` as the 3rd positional arg where `context` is expected.
        The real `context` object is then passed as an unexpected 4th arg.
        """
        try:
            from aspire_orchestrator.skillpacks.mail_ops_desk import MailOpsDeskSkillPack
            import inspect
            impl = getattr(MailOpsDeskSkillPack, "create_mail_account", None)
            assert impl is not None, "create_mail_account method must exist"
            sig = inspect.signature(impl)
            params = list(sig.parameters.keys())
            # Remove 'self' from param list
            params = [p for p in params if p != "self"]
            # If signature has 3 params (domain_name, email_address, context),
            # the wrapper is sending wrong args
            assert len(params) != 3, (
                f"BUG-SP7-01 CONFIRMED: create_mail_account takes {len(params)} params "
                f"({params}) but wrapper calls it with 4 args including password. "
                "Wrapper must be fixed to match implementation signature."
            )
        except ImportError:
            pytest.skip("mail_ops_desk not importable in test environment")

    @pytest.mark.asyncio
    async def test_mail_account_create_with_password_raises_type_error(self):
        """FAILING: wrapper call should raise TypeError due to extra argument.

        This test calls the wrapper and expects a TypeError if the impl signature
        does not accept the 'password' parameter being passed through.
        """
        try:
            from aspire_orchestrator.skillpacks.mail_ops_desk import MailOpsDeskSkillPack

            pack = MailOpsDeskSkillPack.__new__(MailOpsDeskSkillPack)
            ctx = _make_clara_context()

            async def mock_create_mail_account(domain_name, email_address, context):
                return MagicMock(success=True, data={}, receipt={}, error=None)

            pack.create_mail_account = mock_create_mail_account

            # If the wrapper passes the wrong number of args, this should raise TypeError
            with pytest.raises(TypeError):
                await pack.mail_account_create(
                    domain_name="example.com",
                    mailbox_name="info",
                    password="secret123",  # noqa: S106 -- test only
                    context=ctx,
                )
        except ImportError:
            pytest.skip("mail_ops_desk not importable in test environment")


# =============================================================================
# BUG-SP7-02 — CRITICAL
# mail_ops_desk.py: domain_dns_create wrapper drops name and ttl params
# =============================================================================

class TestMailOpsDomainDnsCreateSignatureMismatch:
    """Law #3: Fail Closed — DNS record creation must not silently drop parameters.

    BUG: domain_dns_create(domain_name, record_type, name, value, context, ttl)
    calls create_dns_record(domain_name, record_type, name, value, context, ttl)
    but the implementation at line 401 only accepts:
      (domain_name, record_type, record_value, context)
    The 'name' (subdomain) and 'ttl' parameters are completely absent from the
    implementation — they are silently dropped. DNS record created without correct
    subdomain name is a misconfiguration that will not be caught at runtime.

    Expected: implementation accepts all parameters or wrapper raises if name/ttl
              are required but missing in implementation
    Actual: name and ttl silently dropped, DNS record created with wrong config
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/mail_ops_desk.py
    Lines: 167-177 (wrapper), 401-406 (implementation)
    """

    def test_domain_dns_create_impl_signature_missing_name_and_ttl(self):
        """FAILING: create_dns_record implementation must accept name and ttl params."""
        try:
            from aspire_orchestrator.skillpacks.mail_ops_desk import MailOpsDeskSkillPack
            import inspect
            impl = getattr(MailOpsDeskSkillPack, "create_dns_record", None)
            assert impl is not None, "create_dns_record method must exist"
            sig = inspect.signature(impl)
            params = list(sig.parameters.keys())
            params = [p for p in params if p != "self"]
            assert "name" in params, (
                f"BUG-SP7-02 CONFIRMED: create_dns_record params are {params}. "
                "Missing 'name' (subdomain target). Wrapper passes name but impl ignores it."
            )
            assert "ttl" in params, (
                f"BUG-SP7-02 CONFIRMED: create_dns_record params are {params}. "
                "Missing 'ttl'. Wrapper passes ttl but impl ignores it."
            )
        except ImportError:
            pytest.skip("mail_ops_desk not importable in test environment")


# =============================================================================
# BUG-SP7-03 — CRITICAL
# mail_ops_desk.py: domain_purchase wrapper passes flat args vs dict
# =============================================================================

class TestMailOpsDomainPurchaseSignatureMismatch:
    """Law #3: Fail Closed — domain purchase must fail closed on wrong params.

    BUG: domain_purchase(domain_name, years, contact_email, context) calls
    purchase_domain(domain_name, years, contact_email, context) but the
    implementation signature is (domain_name, registrant_info: dict, context).
    The 'years' int becomes 'registrant_info' dict, 'contact_email' becomes
    'context', and the real 'context' is an unexpected 4th arg.

    Expected: wrapper raises TypeError or fails validation before external call
    Actual: years (int) passed where registrant_info (dict) expected; context
            object passed as positional where it's not expected
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/mail_ops_desk.py
    Lines: 179-186 (wrapper), ~420 (implementation)
    """

    def test_domain_purchase_impl_accepts_registrant_info_dict(self):
        """FAILING: purchase_domain implementation must accept registrant_info as dict."""
        try:
            from aspire_orchestrator.skillpacks.mail_ops_desk import MailOpsDeskSkillPack
            import inspect
            impl = getattr(MailOpsDeskSkillPack, "purchase_domain", None)
            assert impl is not None, "purchase_domain method must exist"
            sig = inspect.signature(impl)
            params = list(sig.parameters.keys())
            params = [p for p in params if p != "self"]
            # Impl should have registrant_info, not years + contact_email
            has_registrant = "registrant_info" in params
            has_years_and_email = ("years" in params and "contact_email" in params)
            assert not (has_registrant and has_years_and_email), (
                "BUG-SP7-03: Wrapper and implementation have different signatures. "
                f"Implementation params: {params}. Wrapper passes (years, contact_email) "
                "but implementation expects registrant_info dict."
            )
            # If registrant_info in impl but not in wrapper, this is a confirmed bug
            if has_registrant and not has_years_and_email:
                assert False, (
                    f"BUG-SP7-03 CONFIRMED: purchase_domain accepts registrant_info dict "
                    f"but wrapper passes flat (years, contact_email) args. "
                    f"Implementation params: {params}"
                )
        except ImportError:
            pytest.skip("mail_ops_desk not importable in test environment")


# =============================================================================
# BUG-SP7-04 — CRITICAL
# teressa_books.py: books_categorize passes dict instead of str transaction_id
# =============================================================================

class TestTeressaBooksCategorizeSignatureMismatch:
    """Law #3: Fail Closed — categorize_transaction must receive a string ID.

    BUG: books_categorize(transaction: dict, context) calls
    categorize_transaction(transaction_id: str, context).
    It passes 'transaction' (a dict) as 'transaction_id' (expected str).
    Inside categorize_transaction, transaction_id.strip() will raise
    AttributeError because dict has no .strip() method.

    Expected: wrapper extracts transaction ID from dict before calling impl,
              or raises a clear validation error
    Actual: AttributeError at transaction_id.strip() when a dict is passed
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/teressa_books.py
    Lines: 136-140 (wrapper calls), 180-ish (implementation)
    """

    @pytest.mark.asyncio
    async def test_books_categorize_with_dict_raises_attribute_error(self):
        """FAILING: books_categorize(dict) must not crash with AttributeError.

        Either the wrapper extracts the ID first, or categorize_transaction
        must accept a dict. Current state: AttributeError on dict.strip().
        """
        try:
            from aspire_orchestrator.skillpacks.teressa_books import TeressaBooksSkillPack
            pack = TeressaBooksSkillPack.__new__(TeressaBooksSkillPack)
            ctx = _make_clara_context()
            transaction_dict = {"id": "txn-123", "amount": 500, "description": "Office supplies"}

            # This should either work (wrapper handles dict) or give a clear error
            # but NOT an unhandled AttributeError inside the implementation
            try:
                result = await pack.books_categorize(transaction=transaction_dict, context=ctx)
                # If it returned successfully, the wrapper must have handled the dict
                assert result is not None
            except AttributeError as e:
                pytest.fail(
                    f"BUG-SP7-04 CONFIRMED: books_categorize(dict) raises AttributeError: {e}. "
                    "Wrapper must extract transaction ID string before calling categorize_transaction."
                )
            except TypeError as e:
                pytest.fail(
                    f"BUG-SP7-04 CONFIRMED: books_categorize(dict) raises TypeError: {e}. "
                    "Wrapper must map dict to string ID before delegating."
                )
        except ImportError:
            pytest.skip("teressa_books not importable in test environment")

    def test_categorize_transaction_impl_requires_string_not_dict(self):
        """PASSING: categorize_transaction implementation expects str transaction_id."""
        try:
            from aspire_orchestrator.skillpacks.teressa_books import TeressaBooksSkillPack
            import inspect
            impl = getattr(TeressaBooksSkillPack, "categorize_transaction", None)
            assert impl is not None, "categorize_transaction method must exist"
            sig = inspect.signature(impl)
            params = sig.parameters
            assert "transaction_id" in params, (
                "categorize_transaction must have 'transaction_id' parameter"
            )
            ann = params["transaction_id"].annotation
            # Annotation should be str (or inspect.Parameter.empty for untyped)
            if ann != inspect.Parameter.empty:
                assert ann is str, (
                    f"BUG-SP7-04: categorize_transaction transaction_id annotation is {ann}, "
                    "but books_categorize wrapper passes a dict. Type mismatch."
                )
        except ImportError:
            pytest.skip("teressa_books not importable in test environment")


# =============================================================================
# BUG-SP7-05 — CRITICAL
# clara_legal.py: contract_sign wrapper passes flat signer args vs signer_info dict
# =============================================================================

class TestClaraLegalContractSignSignatureMismatch:
    """Law #3: Fail Closed — sign_contract must receive signer_info as a dict.

    BUG: contract_sign(contract_id, signer_name, signer_email, context) calls
    sign_contract(contract_id=..., signer_name=..., signer_email=..., context=...)
    but sign_contract signature is (contract_id, signer_info: dict, context).
    The keyword args 'signer_name' and 'signer_email' do not match the
    'signer_info' parameter — Python will raise TypeError on unexpected kwargs.

    Expected: wrapper builds signer_info dict and passes it correctly
    Actual: TypeError from unexpected keyword arguments
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/clara_legal.py
    Lines: 301-302 (wrapper), 721-726 (implementation)
    """

    def test_sign_contract_impl_takes_signer_info_dict_not_flat_args(self):
        """FAILING: sign_contract implementation expects signer_info dict, not flat args."""
        try:
            from aspire_orchestrator.skillpacks.clara_legal import ClaraLegalSkillPack
            import inspect
            impl = getattr(ClaraLegalSkillPack, "sign_contract", None)
            assert impl is not None, "sign_contract method must exist"
            sig = inspect.signature(impl)
            params = list(sig.parameters.keys())
            params = [p for p in params if p != "self"]
            # Implementation should have signer_info, not signer_name + signer_email
            has_signer_info = "signer_info" in params
            has_flat = "signer_name" in params and "signer_email" in params
            # Wrapper sends flat kwargs but impl has signer_info -> confirmed mismatch
            if has_signer_info and not has_flat:
                assert False, (
                    f"BUG-SP7-05 CONFIRMED: sign_contract accepts (contract_id, signer_info: dict, context) "
                    f"but contract_sign wrapper calls it with flat signer_name and signer_email keyword args. "
                    f"Implementation params: {params}. This will raise TypeError at runtime."
                )
        except ImportError:
            pytest.skip("clara_legal not importable in test environment")

    @pytest.mark.asyncio
    async def test_contract_sign_wrapper_raises_type_error_with_flat_args(self):
        """FAILING: contract_sign wrapper call should raise TypeError."""
        try:
            from aspire_orchestrator.skillpacks.clara_legal import ClaraLegalSkillPack
            pack = ClaraLegalSkillPack.__new__(ClaraLegalSkillPack)
            ctx = _make_clara_context()

            # Stub sign_contract with the correct signature
            async def correct_sign_contract(contract_id: str, signer_info: dict, context: Any):
                return MagicMock(success=True, data={}, receipt={})

            pack.sign_contract = correct_sign_contract

            # Wrapper calls with flat args — should fail with TypeError
            with pytest.raises(TypeError):
                await pack.contract_sign(
                    contract_id="doc-123",
                    signer_name="John Doe",
                    signer_email="john@example.com",
                    context=ctx,
                )
        except ImportError:
            pytest.skip("clara_legal not importable in test environment")


# =============================================================================
# BUG-SP7-06 — HIGH
# Systematic LAW #2: Rule-based skillpack classes never call store_receipts()
# =============================================================================

class TestRuleBasedSkillPacksReceiptPersistence:
    """Law #2: Receipt for ALL actions — receipts must be persisted, not just returned.

    BUG: All rule-based skillpack classes (_make_receipt / _emit_receipt helpers)
    build receipt dicts and include them in SkillPackResult but NEVER call
    store_receipts() to persist them. Receipts are only persisted if the caller
    explicitly persists them — there is no guarantee they are stored.

    Affected files:
      - adam_research.py: AdamResearchSkillPack (all methods)
      - eli_inbox.py: EliInboxSkillPack (all methods)
      - milo_payroll.py: MiloPayrollSkillPack (all methods)
      - quinn_invoicing.py: QuinnInvoicingSkillPack (all methods)
      - teressa_books.py: TeressaBooksSkillPack (all methods)
      - sarah_front_desk.py: SarahFrontDeskSkillPack (all methods)
      - nora_conference.py: NoraConferenceSkillPack (all methods)
      - mail_ops_desk.py: MailOpsDeskSkillPack (all methods)
      - clara_legal.py: ClaraLegalSkillPack (all methods)

    Expected: store_receipts() called inside each method after building the receipt
    Actual: receipt returned in SkillPackResult only — not persisted
    """

    def _get_store_receipts_callsites(self, source_text: str) -> list[str]:
        """Find lines in source that call store_receipts()."""
        lines = source_text.splitlines()
        return [line.strip() for line in lines if "store_receipts" in line]

    def test_adam_research_rule_based_class_calls_store_receipts(self):
        """FAILING: AdamResearchSkillPack must call store_receipts() to persist receipts."""
        import ast
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/adam_research.py"
            )
            with open(path) as f:
                source = f.read()
            calls = self._get_store_receipts_callsites(source)
            assert len(calls) > 0, (
                "BUG-SP7-06 CONFIRMED: adam_research.py AdamResearchSkillPack never calls "
                "store_receipts(). Receipts from rule-based class are not persisted (Law #2)."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")

    def test_eli_inbox_rule_based_class_calls_store_receipts(self):
        """FAILING: EliInboxSkillPack must call store_receipts() to persist receipts."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/eli_inbox.py"
            )
            with open(path) as f:
                source = f.read()
            calls = self._get_store_receipts_callsites(source)
            assert len(calls) > 0, (
                "BUG-SP7-06 CONFIRMED: eli_inbox.py EliInboxSkillPack never calls "
                "store_receipts(). Receipts not persisted (Law #2)."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")

    def test_milo_payroll_rule_based_class_calls_store_receipts(self):
        """FAILING: MiloPayrollSkillPack must call store_receipts() to persist receipts."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/milo_payroll.py"
            )
            with open(path) as f:
                source = f.read()
            calls = self._get_store_receipts_callsites(source)
            assert len(calls) > 0, (
                "BUG-SP7-06 CONFIRMED: milo_payroll.py MiloPayrollSkillPack never calls "
                "store_receipts(). Receipts not persisted (Law #2)."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")

    def test_quinn_invoicing_rule_based_class_calls_store_receipts(self):
        """FAILING: QuinnInvoicingSkillPack must call store_receipts() to persist receipts."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/quinn_invoicing.py"
            )
            with open(path) as f:
                source = f.read()
            calls = self._get_store_receipts_callsites(source)
            assert len(calls) > 0, (
                "BUG-SP7-06 CONFIRMED: quinn_invoicing.py QuinnInvoicingSkillPack never calls "
                "store_receipts(). Receipts not persisted (Law #2)."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")

    def test_teressa_books_rule_based_class_calls_store_receipts(self):
        """FAILING: TeressaBooksSkillPack must call store_receipts() to persist receipts."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/teressa_books.py"
            )
            with open(path) as f:
                source = f.read()
            calls = self._get_store_receipts_callsites(source)
            assert len(calls) > 0, (
                "BUG-SP7-06 CONFIRMED: teressa_books.py TeressaBooksSkillPack never calls "
                "store_receipts(). Receipts not persisted (Law #2)."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")

    def test_clara_legal_rule_based_class_calls_store_receipts(self):
        """FAILING: ClaraLegalSkillPack must call store_receipts() to persist receipts."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/clara_legal.py"
            )
            with open(path) as f:
                source = f.read()
            calls = self._get_store_receipts_callsites(source)
            assert len(calls) > 0, (
                "BUG-SP7-06 CONFIRMED: clara_legal.py ClaraLegalSkillPack never calls "
                "store_receipts(). Receipts not persisted (Law #2)."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")


# =============================================================================
# BUG-SP7-07 — HIGH
# EnhancedMiloPayroll.initiate_dual_approval is sync, returns dict, no receipt
# =============================================================================

class TestEnhancedMiloPayrollDualApprovalGovernance:
    """Law #2 + Law #4: RED-tier dual approval must be async and emit a receipt.

    BUG: EnhancedMiloPayroll.initiate_dual_approval is:
      - Synchronous (def, not async def)
      - Returns a plain dict, not AgentResult
      - Emits NO receipt for the dual approval initiation action itself

    This means a RED-tier payroll authorization action produces no audit trail.

    Expected: async def, returns AgentResult, calls self.emit_receipt()
    Actual: sync def, returns dict, no receipt emitted
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/milo_payroll.py
    Line: 754
    """

    def test_initiate_dual_approval_is_async(self):
        """FAILING: initiate_dual_approval must be async def to use await."""
        try:
            from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
            import asyncio
            impl = getattr(EnhancedMiloPayroll, "initiate_dual_approval", None)
            assert impl is not None, "initiate_dual_approval method must exist"
            assert asyncio.iscoroutinefunction(impl), (
                "BUG-SP7-07 CONFIRMED: EnhancedMiloPayroll.initiate_dual_approval is NOT async. "
                "RED-tier dual approval must be async and return AgentResult."
            )
        except ImportError:
            pytest.skip("milo_payroll not importable in test environment")

    def test_initiate_dual_approval_returns_agent_result(self):
        """FAILING: initiate_dual_approval must return AgentResult, not dict."""
        try:
            from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
            import asyncio
            import inspect
            impl = getattr(EnhancedMiloPayroll, "initiate_dual_approval", None)
            assert impl is not None
            sig = inspect.signature(impl)
            return_ann = sig.return_annotation
            if return_ann != inspect.Parameter.empty:
                assert return_ann.__name__ != "dict", (
                    "BUG-SP7-07 CONFIRMED: initiate_dual_approval return annotation is dict. "
                    "Must return AgentResult for governance compliance."
                )
        except ImportError:
            pytest.skip("milo_payroll not importable in test environment")

    def test_initiate_dual_approval_source_contains_emit_receipt(self):
        """FAILING: initiate_dual_approval must call emit_receipt or build_receipt."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/milo_payroll.py"
            )
            with open(path) as f:
                source = f.read()

            # Find the method body
            start = source.find("def initiate_dual_approval")
            if start == -1:
                pytest.skip("initiate_dual_approval not found in source")

            # Get the next ~30 lines after the method definition
            method_snippet = source[start:start + 800]
            has_receipt = "emit_receipt" in method_snippet or "build_receipt" in method_snippet
            assert has_receipt, (
                "BUG-SP7-07 CONFIRMED: EnhancedMiloPayroll.initiate_dual_approval does not "
                "call emit_receipt() or build_receipt(). RED-tier dual approval action produces "
                "no audit trail (Law #2 violation)."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")


# =============================================================================
# BUG-SP7-08 — HIGH
# EnhancedClaraLegal.initiate_dual_approval is sync, returns dict, no receipt
# =============================================================================

class TestEnhancedClaraLegalDualApprovalGovernance:
    """Law #2 + Law #4: RED-tier dual approval must be async and emit a receipt.

    BUG: EnhancedClaraLegal.initiate_dual_approval is:
      - Synchronous (def, not async def)
      - Returns a plain dict, not AgentResult
      - Emits NO receipt for the dual approval initiation action itself

    Expected: async def, returns AgentResult, calls self.emit_receipt()
    Actual: sync def, returns dict, no receipt emitted
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/clara_legal.py
    Line: 1265
    """

    def test_clara_initiate_dual_approval_is_async(self):
        """FAILING: EnhancedClaraLegal.initiate_dual_approval must be async def."""
        try:
            from aspire_orchestrator.skillpacks.clara_legal import EnhancedClaraLegal
            import asyncio
            impl = getattr(EnhancedClaraLegal, "initiate_dual_approval", None)
            assert impl is not None, "initiate_dual_approval method must exist"
            assert asyncio.iscoroutinefunction(impl), (
                "BUG-SP7-08 CONFIRMED: EnhancedClaraLegal.initiate_dual_approval is NOT async. "
                "RED-tier contract signing approval must be async and return AgentResult."
            )
        except ImportError:
            pytest.skip("clara_legal not importable in test environment")

    def test_clara_initiate_dual_approval_source_contains_emit_receipt(self):
        """FAILING: initiate_dual_approval must call emit_receipt."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/clara_legal.py"
            )
            with open(path) as f:
                source = f.read()

            start = source.find("def initiate_dual_approval", source.find("EnhancedClaraLegal"))
            if start == -1:
                pytest.skip("initiate_dual_approval not found in EnhancedClaraLegal source")

            method_snippet = source[start:start + 800]
            has_receipt = "emit_receipt" in method_snippet or "build_receipt" in method_snippet
            assert has_receipt, (
                "BUG-SP7-08 CONFIRMED: EnhancedClaraLegal.initiate_dual_approval does not call "
                "emit_receipt(). RED-tier contract signing action produces no audit trail (Law #2)."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")


# =============================================================================
# BUG-SP7-09 — MEDIUM
# ava_user.py: AvaUserSkillPack is a dead empty class
# =============================================================================

class TestAvaUserSkillPackDeadClass:
    """Dead code: AvaUserSkillPack declared but has no methods.

    BUG: AvaUserSkillPack is declared at line 13 but has no body — only a
    docstring-like string that Python interprets as the body of EnhancedAvaUser
    (the next class). AvaUserSkillPack has no methods and cannot be used.

    Expected: AvaUserSkillPack either has methods or is removed
    Actual: Empty class with no callable interface
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/ava_user.py
    Line: 13
    """

    def test_ava_user_skill_pack_has_callable_methods(self):
        """FAILING: AvaUserSkillPack must have at least one callable method."""
        try:
            from aspire_orchestrator.skillpacks.ava_user import AvaUserSkillPack
            import inspect
            methods = [
                name for name, _ in inspect.getmembers(AvaUserSkillPack, predicate=inspect.isfunction)
                if not name.startswith("_")
            ]
            assert len(methods) > 0, (
                f"BUG-SP7-09 CONFIRMED: AvaUserSkillPack has NO public methods. "
                "Class is declared but is effectively dead code. "
                "Either add methods or remove the empty class declaration."
            )
        except ImportError:
            pytest.skip("ava_user not importable in test environment")


# =============================================================================
# BUG-SP7-11 — MEDIUM
# teressa_books.py: books_sync always passes empty date_range dict
# =============================================================================

class TestTeressaBooksBooksSyncDateRange:
    """Law #3: Fail Closed — books_sync must not silently pass empty date_range.

    BUG: books_sync(account_id, date_range, context) calls
    sync_books(account_id, date_range={}, context) — always passes empty dict
    regardless of what date_range the caller provided. sync_books internally
    checks for 'start' and 'end' keys and returns error "Missing required
    parameter: date_range" when the dict is empty.

    Expected: wrapper passes the caller's date_range through to impl
    Actual: date_range is always overridden with {} — every call fails
    File: backend/orchestrator/src/aspire_orchestrator/skillpacks/teressa_books.py
    Line: 133
    """

    def test_books_sync_wrapper_passes_caller_date_range(self):
        """FAILING: books_sync must forward the caller-provided date_range, not {}."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/teressa_books.py"
            )
            with open(path) as f:
                source = f.read()

            # Find books_sync method
            start = source.find("async def books_sync")
            if start == -1:
                pytest.skip("books_sync not found in source")

            method_snippet = source[start:start + 300]
            # Check if the wrapper hardcodes date_range={}
            assert "date_range={}" not in method_snippet, (
                "BUG-SP7-11 CONFIRMED: books_sync wrapper calls sync_books with date_range={} "
                "(hardcoded empty dict), ignoring the caller's date_range parameter. "
                "Every books_sync call will fail with 'Missing required parameter: date_range'."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")

    @pytest.mark.asyncio
    async def test_books_sync_with_valid_date_range_does_not_fail_validation(self):
        """FAILING: books_sync with valid date_range must not trigger 'missing date_range' error."""
        try:
            from aspire_orchestrator.skillpacks.teressa_books import TeressaBooksSkillPack
            pack = TeressaBooksSkillPack.__new__(TeressaBooksSkillPack)
            ctx = _make_clara_context()

            captured_date_range = {}

            async def mock_sync_books(account_id, date_range, context):
                captured_date_range.update(date_range)
                if not date_range.get("start") or not date_range.get("end"):
                    return MagicMock(
                        success=False, data=None, receipt={},
                        error="Missing required parameter: date_range",
                    )
                return MagicMock(success=True, data={"synced": 10}, receipt={}, error=None)

            pack.sync_books = mock_sync_books
            result = await pack.books_sync(
                account_id="acct-123",
                date_range={"start": "2026-01-01", "end": "2026-03-31"},
                context=ctx,
            )

            assert result.success is True, (
                f"BUG-SP7-11 CONFIRMED: books_sync returned success=False with error "
                f"'{result.error}'. The wrapper is passing empty date_range={{}} to impl, "
                "ignoring the caller's provided date_range."
            )
            assert captured_date_range.get("start") == "2026-01-01", (
                "BUG-SP7-11 CONFIRMED: books_sync did not forward the caller's date_range. "
                f"Implementation received: {captured_date_range}"
            )
        except ImportError:
            pytest.skip("teressa_books not importable in test environment")


# =============================================================================
# BUG-SP7-12 — LOW
# Scaffold packs: _handle_read_action is dead code (never called)
# =============================================================================

class TestScaffoldPackDeadCode:
    """Dead code: _handle_read_action defined but never called in scaffold packs.

    BUG: All 4 scaffold packs (qa_evals, release_manager, security_review,
    sre_triage) define a _handle_read_action method but all action dispatch
    routes through _handle_write_action. _handle_read_action is never invoked.

    Expected: either _handle_read_action is called for read operations,
              or it is removed as dead code
    Actual: dead method present in all 4 scaffold packs
    """

    @pytest.mark.parametrize("pack_name", [
        "qa_evals",
        "release_manager",
        "security_review",
        "sre_triage",
    ])
    def test_scaffold_pack_has_no_dead_handle_read_action(self, pack_name: str):
        """LOW: _handle_read_action exists but is never called — should be removed or wired up."""
        try:
            path = (
                f"C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                f"aspire_orchestrator/skillpacks/{pack_name}.py"
            )
            with open(path) as f:
                source = f.read()

            has_definition = "def _handle_read_action" in source
            has_call = "_handle_read_action(" in source

            if has_definition:
                assert has_call, (
                    f"BUG-SP7-12: {pack_name}.py defines _handle_read_action but never calls it. "
                    "This is dead code. Either wire it into the action dispatch or remove it."
                )
        except FileNotFoundError:
            pytest.skip(f"{pack_name}.py not found in expected location")


# =============================================================================
# BUG-SP7-13 — LOW
# Unused imports in tec_documents.py and nora_conference.py
# =============================================================================

class TestUnusedImports:
    """Unused imports pollute namespace and can cause confusion.

    BUG: RiskTier imported but unused in:
      - tec_documents.py line 37: from aspire_orchestrator.models import Outcome, RiskTier
      - nora_conference.py line 31: from aspire_orchestrator.models import Outcome, ReceiptType, RiskTier
    Both ReceiptType and RiskTier are unused in nora_conference.py.
    """

    def test_tec_documents_risktier_import_is_used(self):
        """LOW: RiskTier import in tec_documents.py must be used or removed."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/tec_documents.py"
            )
            with open(path) as f:
                source = f.read()

            import_line = [l for l in source.splitlines() if "RiskTier" in l and "import" in l]
            if not import_line:
                return  # Not imported — clean

            # Check if RiskTier is used after the import
            after_import = source[source.find("RiskTier") + len("RiskTier"):]
            usage_count = after_import.count("RiskTier")
            assert usage_count > 0, (
                "BUG-SP7-13: RiskTier is imported in tec_documents.py but never used. "
                "Remove the unused import."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")

    def test_nora_conference_receipt_type_import_is_used(self):
        """LOW: ReceiptType import in nora_conference.py must be used or removed."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/nora_conference.py"
            )
            with open(path) as f:
                source = f.read()

            import_line = [l for l in source.splitlines() if "ReceiptType" in l and "import" in l]
            if not import_line:
                return  # Not imported — clean

            # Check if ReceiptType is used after the import declaration
            import_pos = source.find("ReceiptType")
            after_import = source[import_pos + len("ReceiptType"):]
            usage_count = after_import.count("ReceiptType")
            assert usage_count > 0, (
                "BUG-SP7-13: ReceiptType is imported in nora_conference.py but never used. "
                "Remove the unused import."
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")


# =============================================================================
# POSITIVE TESTS — Confirm correct behaviors still work
# =============================================================================

class TestSkillpackGovernancePositiveCases:
    """Positive tests confirming correct governance patterns are in place."""

    def test_enhanced_clara_legal_inherits_agentic_skill_pack(self):
        """PASSING: EnhancedClaraLegal must extend AgenticSkillPack."""
        try:
            from aspire_orchestrator.skillpacks.clara_legal import EnhancedClaraLegal
            from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
            assert issubclass(EnhancedClaraLegal, AgenticSkillPack), (
                "EnhancedClaraLegal must extend AgenticSkillPack"
            )
        except ImportError:
            pytest.skip("clara_legal not importable in test environment")

    def test_enhanced_milo_payroll_inherits_agentic_skill_pack(self):
        """PASSING: EnhancedMiloPayroll must extend AgenticSkillPack."""
        try:
            from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
            from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
            assert issubclass(EnhancedMiloPayroll, AgenticSkillPack), (
                "EnhancedMiloPayroll must extend AgenticSkillPack"
            )
        except ImportError:
            pytest.skip("milo_payroll not importable in test environment")

    def test_enhanced_ava_user_inherits_agentic_skill_pack(self):
        """PASSING: EnhancedAvaUser must extend AgenticSkillPack."""
        try:
            from aspire_orchestrator.skillpacks.ava_user import EnhancedAvaUser
            from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
            assert issubclass(EnhancedAvaUser, AgenticSkillPack), (
                "EnhancedAvaUser must extend AgenticSkillPack"
            )
        except ImportError:
            pytest.skip("ava_user not importable in test environment")

    def test_enhanced_ava_user_has_intent_classify_method(self):
        """PASSING: EnhancedAvaUser must expose intent_classify."""
        try:
            from aspire_orchestrator.skillpacks.ava_user import EnhancedAvaUser
            assert hasattr(EnhancedAvaUser, "intent_classify"), (
                "EnhancedAvaUser missing intent_classify method"
            )
        except ImportError:
            pytest.skip("ava_user not importable in test environment")

    def test_clara_legal_sign_contract_masks_pii_in_receipt(self):
        """PASSING: sign_contract must mask signer_name and signer_email in receipt inputs."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/clara_legal.py"
            )
            with open(path) as f:
                source = f.read()

            # Find sign_contract method and check for _mask_name and _mask_email
            start = source.find("async def sign_contract")
            if start == -1:
                pytest.skip("sign_contract not found")
            method_snippet = source[start:start + 1500]
            assert "_mask_name(" in method_snippet, (
                "sign_contract must call _mask_name() to redact PII in receipt (Law #9)"
            )
            assert "_mask_email(" in method_snippet, (
                "sign_contract must call _mask_email() to redact PII in receipt (Law #9)"
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")

    def test_generate_contract_fails_closed_on_missing_jurisdiction(self):
        """PASSING: generate_contract must deny when jurisdiction_required and no jurisdiction_state."""
        try:
            path = (
                "C:/Users/tonio/Projects/myapp/backend/orchestrator/src/"
                "aspire_orchestrator/skillpacks/clara_legal.py"
            )
            with open(path) as f:
                source = f.read()

            start = source.find("async def generate_contract")
            if start == -1:
                pytest.skip("generate_contract not found")
            method_snippet = source[start:start + 2000]
            assert "MISSING_JURISDICTION" in method_snippet, (
                "generate_contract must emit MISSING_JURISDICTION denial when "
                "jurisdiction is required but absent (Law #3: Fail Closed)"
            )
        except FileNotFoundError:
            pytest.skip("File not found in expected location")
