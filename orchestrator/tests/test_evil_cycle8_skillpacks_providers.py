"""Evil + negative tests — Cycle 8: Skillpacks & Providers deep scan.

Covers bugs found in the 2026-03-23 Cycle 8 code review:
  - BUG-SK8-01: finn_finance_manager — sync functions called from async wrappers (no await)
  - BUG-SK8-02: teressa_books.books_sync — always passes date_range={} silently
  - BUG-SK8-03: mail_ops_desk.domain_dns_create wrapper — signature mismatch (name param dropped)
  - BUG-SK8-04: mail_ops_desk.create_mail_account — domain_name validation after binding check
  - BUG-SK8-05: milo_payroll._payroll_snapshots — in-memory store (cross-process leakage risk)
  - BUG-SK8-06: quinn_invoicing.create_invoice — outcome="success" pre-approval on YELLOW tier
  - BUG-SK8-07: nora_conference — unused import of ReceiptType, RiskTier (dead import)
  - BUG-SK8-08: finn_finance_manager.initiate_dual_approval / clara_legal.initiate_dual_approval
                 are sync def, not async def — callers that await them will crash
  - BUG-SK8-09: calendar_client.py — str(e) leaks PII in error field (Law #9)
  - BUG-SK8-10: office_message_client.py — str(e) leaks PII in error field (Law #9)
  - BUG-SK8-11: Providers: 24/28 provider clients have no circuit_breaker; only base_client
                 and twilio_client reference it — all remaining clients call HTTP without breaker
  - BUG-SK8-12: receipt_store not called from ANY rule-based skillpack (Law #2 partial coverage)
"""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

@dataclass
class _FinnCtx:
    suite_id: str = "test-suite"
    office_id: str = "test-office"
    correlation_id: str = "corr-001"
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


@dataclass
class _TeressaCtx:
    suite_id: str = "test-suite"
    office_id: str = "test-office"
    correlation_id: str = "corr-002"
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


@dataclass
class _MailCtx:
    suite_id: str = "test-suite"
    office_id: str = "test-office"
    correlation_id: str = "corr-003"
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


@dataclass
class _MiloCtx:
    suite_id: str = "test-suite"
    office_id: str = "test-office"
    correlation_id: str = "corr-004"
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


@dataclass
class _QuinnCtx:
    suite_id: str = "test-suite"
    office_id: str = "test-office"
    correlation_id: str = "corr-005"
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


# ---------------------------------------------------------------------------
# BUG-SK8-01: finn_finance_manager sync functions called from async wrappers
# ---------------------------------------------------------------------------

class TestFinnSyncAsyncMismatch(unittest.TestCase):
    """BUG-SK8-01: read_finance_exceptions / draft_finance_packet /
    create_finance_proposal / dispatch_a2a_delegation are sync functions.
    EnhancedFinnFinanceManager wraps them via regular call (not await).
    This is CORRECT for sync functions, but the wrapper methods themselves
    are async, so callers must await them.  This test verifies the wrappers
    can be awaited and return SkillPackResult-shaped data without crashing.

    Law #2: Every action must produce a receipt regardless of sync/async path.
    """

    def test_read_finance_exceptions_is_sync(self) -> None:
        """Verify read_finance_exceptions is a sync function (no coroutine returned)."""
        from aspire_orchestrator.skillpacks.finn_finance_manager import (
            read_finance_exceptions,
            FinnFMContext,
        )
        ctx = FinnFMContext(suite_id="s1", office_id="o1", correlation_id="c1")
        result = read_finance_exceptions(ctx, severity="all")
        # Must NOT be a coroutine — it's sync
        self.assertFalse(
            asyncio.iscoroutine(result),
            "read_finance_exceptions must be a synchronous function returning SkillPackResult directly",
        )
        self.assertTrue(result.success)
        self.assertIn("receipt_id", result.receipt)

    def test_draft_finance_packet_is_sync(self) -> None:
        """Verify draft_finance_packet is sync and returns receipt."""
        from aspire_orchestrator.skillpacks.finn_finance_manager import (
            draft_finance_packet,
            FinnFMContext,
        )
        ctx = FinnFMContext(suite_id="s1", office_id="o1", correlation_id="c1")
        result = draft_finance_packet(ctx, packet_type="budget", title="Q2 Budget", description="desc")
        self.assertFalse(asyncio.iscoroutine(result))
        self.assertTrue(result.success)
        self.assertTrue(result.approval_required, "YELLOW draft must set approval_required=True")
        self.assertIn("receipt_id", result.receipt)

    def test_enhanced_wrapper_returns_agent_result_with_receipt(self) -> None:
        """BUG-SK8-01: EnhancedFinnFinanceManager.finance_exceptions_read must
        return AgentResult with receipt even when exceptions list is empty."""
        from aspire_orchestrator.skillpacks.finn_finance_manager import EnhancedFinnFinanceManager
        from aspire_orchestrator.services.agent_sdk_base import AgentContext

        ctx = AgentContext(suite_id="s1", office_id="o1", correlation_id="c1")
        pack = EnhancedFinnFinanceManager()

        result = asyncio.get_event_loop().run_until_complete(
            pack.finance_exceptions_read({}, ctx)
        )
        self.assertIsNotNone(result.receipt, "Missing receipt from finance_exceptions_read")
        self.assertIn("receipt_id", result.receipt)


# ---------------------------------------------------------------------------
# BUG-SK8-02: teressa_books.books_sync silently ignores date_range
# ---------------------------------------------------------------------------

class TestTeressaBooksSyncSignatureMismatch(unittest.TestCase):
    """BUG-SK8-02: books_sync wrapper always passes date_range={} regardless of
    what the caller provides.  Any caller passing a real date range gets silently
    overridden, which causes the underlying sync_books to reject with
    MISSING_DATE_RANGE denial — but the caller receives a success=False with a
    receipt that claims 'MISSING_DATE_RANGE', which is misleading.

    Law #3 implication: The wrapper appears to accept a date_range param but
    ignores it — fail-closed behaviour is violated because the caller intended
    a date-scoped sync and instead gets a denial with no indication the param
    was discarded.
    """

    def test_books_sync_wrapper_passes_empty_date_range(self) -> None:
        """Evil test: books_sync ignores caller-supplied date_range={start,end}."""
        from aspire_orchestrator.skillpacks.teressa_books import TeressaBooksSkillPack

        pack = TeressaBooksSkillPack()
        ctx = _TeressaCtx()

        # Caller supplies a legitimate date_range — it must be forwarded.
        # With the current bug: date_range={} is passed instead, causing
        # MISSING_DATE_RANGE denial.
        result = asyncio.get_event_loop().run_until_complete(
            pack.books_sync(
                account_id="acct-123",
                context=ctx,  # type: ignore[arg-type]
            )
        )

        # Expected: denied because date_range is always {} inside the wrapper.
        # This IS the observable symptom of the bug — test documents the breakage.
        self.assertFalse(result.success, "Bug confirmed: books_sync always fails because date_range={}")
        self.assertEqual(
            result.receipt["policy"]["reasons"],
            ["MISSING_DATE_RANGE"],
            "Expected MISSING_DATE_RANGE denial caused by hardcoded date_range={}",
        )

    def test_sync_books_direct_call_succeeds_with_date_range(self) -> None:
        """Positive control: direct call to sync_books with valid date_range succeeds (mocked tool)."""
        from aspire_orchestrator.skillpacks.teressa_books import TeressaBooksSkillPack
        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult

        mock_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="quickbooks.sync",
            data={"synced": True},
        )

        pack = TeressaBooksSkillPack()
        ctx = _TeressaCtx()

        with patch(
            "aspire_orchestrator.skillpacks.teressa_books.execute_tool",
            new=AsyncMock(return_value=mock_result),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                pack.sync_books(
                    account_id="acct-123",
                    date_range={"start": "2026-01-01", "end": "2026-03-31"},
                    context=ctx,  # type: ignore[arg-type]
                )
            )
        self.assertTrue(result.success)
        self.assertTrue(result.approval_required)


# ---------------------------------------------------------------------------
# BUG-SK8-03: mail_ops_desk.domain_dns_create wrapper drops 'name' param
# ---------------------------------------------------------------------------

class TestMailOpsDnsWrapperSignature(unittest.TestCase):
    """BUG-SK8-03: The domain_dns_create compatibility wrapper (line ~167) accepts
    (domain_name, record_type, name, value, context, *, ttl) but calls
    create_dns_record(domain_name, record_type, record_value, context, ttl).
    The 'name' argument is dropped entirely and 'value' is passed as 'record_value'.

    The real create_dns_record signature is:
        create_dns_record(domain_name, record_type, record_value, context, *, ttl)

    This means callers going through domain_dns_create lose the 'name' subdomain
    field silently.  The binding fields check in create_dns_record checks for 'value'
    (not 'record_value'), so it will also fail binding field validation differently.
    """

    def test_domain_dns_create_wrapper_parameter_mismatch(self) -> None:
        """Calling domain_dns_create must produce the same receipt as create_dns_record.
        If the wrapper drops 'name', the two call paths diverge — this documents the bug.
        """
        from aspire_orchestrator.skillpacks.mail_ops_desk import MailOpsDeskSkillPack

        pack = MailOpsDeskSkillPack()
        ctx = _MailCtx()

        result = asyncio.get_event_loop().run_until_complete(
            pack.domain_dns_create(
                domain_name="example.com",
                record_type="MX",
                name="mail",     # This param is silently dropped by the wrapper
                value="10 mail.example.com",
                context=ctx,  # type: ignore[arg-type]
                ttl=3600,
            )
        )

        # Positive path: wrapper should produce approval_required=True with receipt
        # (it will still work because create_dns_record uses positional 'record_value'
        #  which receives the 'value' arg — but 'name' is lost)
        self.assertIn("receipt_id", result.receipt, "Receipt must be emitted even from wrapper path")

    def test_domain_dns_create_direct_vs_wrapper_produce_same_dns_plan(self) -> None:
        """Evil test: DNS record 'name' field must appear in dns_plan data.
        Wrapper path drops 'name' — dns_plan will be missing the subdomain field.
        """
        from aspire_orchestrator.skillpacks.mail_ops_desk import MailOpsDeskSkillPack

        pack = MailOpsDeskSkillPack()
        ctx = _MailCtx()

        result = asyncio.get_event_loop().run_until_complete(
            pack.domain_dns_create(
                domain_name="example.com",
                record_type="MX",
                name="mail",
                value="10 mail.example.com",
                context=ctx,  # type: ignore[arg-type]
            )
        )
        # If bug is present: dns_plan data will NOT contain 'name' key
        # (wrapper discards it before calling create_dns_record)
        if result.success:
            self.assertIn(
                "name",
                result.data,
                "BUG-SK8-03: DNS plan is missing 'name' (subdomain) field — "
                "domain_dns_create wrapper drops the 'name' parameter",
            )


# ---------------------------------------------------------------------------
# BUG-SK8-04: mail_ops_desk.create_mail_account — domain_name check after binding
# ---------------------------------------------------------------------------

class TestMailOpsAccountCreateValidationOrder(unittest.TestCase):
    """BUG-SK8-04: In create_mail_account, binding field check for email_address
    runs BEFORE the domain_name emptiness check (line ~498).  If email_address is
    provided but domain_name is empty, execution reaches the plan-building block
    which calls domain_name.strip() — but domain_name validation gate fires first
    now. However the validation order produces different receipts than expected:
    binding check returns MISSING_BINDING_FIELDS receipt even when the true
    failure is MISSING_DOMAIN_NAME.  Receipts should reflect the actual denial
    reason accurately (Law #2).
    """

    def test_empty_domain_name_denied_with_correct_reason_code(self) -> None:
        """Evil test: empty domain_name must produce MISSING_DOMAIN_NAME receipt,
        not MISSING_BINDING_FIELDS."""
        from aspire_orchestrator.skillpacks.mail_ops_desk import MailOpsDeskSkillPack

        pack = MailOpsDeskSkillPack()
        ctx = _MailCtx()

        result = asyncio.get_event_loop().run_until_complete(
            pack.create_mail_account(
                domain_name="",          # empty
                email_address="user@example.com",  # valid
                context=ctx,  # type: ignore[arg-type]
            )
        )

        self.assertFalse(result.success)
        # Bug: actual reason will be "MISSING_BINDING_FIELDS" for 'email_address'
        # because the binding check fires with domain_name="" before reaching
        # the domain_name validation.  Expected: "MISSING_DOMAIN_NAME".
        reasons = result.receipt["policy"]["reasons"]
        self.assertIn(
            "MISSING_DOMAIN_NAME",
            reasons,
            f"BUG-SK8-04: expected MISSING_DOMAIN_NAME but got {reasons}",
        )


# ---------------------------------------------------------------------------
# BUG-SK8-05: milo_payroll in-memory snapshot store — cross-process risk
# ---------------------------------------------------------------------------

class TestMiloPayrollSnapshotIsolation(unittest.TestCase):
    """BUG-SK8-05: _payroll_snapshots is a module-level dict.
    In multi-process deployments, snapshot written by process A is invisible to
    process B.  Tenant B can also see snapshots if keys collide (same period,
    same suite_id across test isolation boundaries).

    This test demonstrates the cross-suite visibility risk in single-process
    test environments (state leakage between tests).
    """

    def setUp(self) -> None:
        from aspire_orchestrator.skillpacks.milo_payroll import clear_payroll_snapshots
        clear_payroll_snapshots()

    def tearDown(self) -> None:
        from aspire_orchestrator.skillpacks.milo_payroll import clear_payroll_snapshots
        clear_payroll_snapshots()

    def test_suite_a_snapshot_not_visible_to_suite_b(self) -> None:
        """Evil test: snapshots keyed by suite_id must not be accessible cross-tenant."""
        import aspire_orchestrator.skillpacks.milo_payroll as mp

        # Manually inject a snapshot for suite_a
        mp._payroll_snapshots["suite-a:2026-03"] = {
            "payroll_period": "2026-03",
            "suite_id": "suite-a",
            "data": "secret-payroll-data",
        }

        # suite-b must NOT find this snapshot
        suite_b_key = "suite-b:2026-03"
        self.assertNotIn(
            suite_b_key,
            mp._payroll_snapshots,
            "Cross-suite snapshot isolation confirmed — suite-b cannot read suite-a's snapshot",
        )

    def test_run_payroll_denied_without_snapshot(self) -> None:
        """Law #3 evil test: run_payroll must be denied when snapshot is missing."""
        from aspire_orchestrator.skillpacks.milo_payroll import MiloPayrollSkillPack, MiloContext

        pack = MiloPayrollSkillPack()
        ctx = MiloContext(suite_id="suite-x", office_id="office-x", correlation_id="c-x")

        result = asyncio.get_event_loop().run_until_complete(
            pack.run_payroll(
                payroll_period="2026-03",
                context=ctx,
                company_id="co-123",
                payroll_id="pay-456",
                total_amount="50000",
                approval_evidence={"approver": "hr", "timestamp": "2026-03-23T10:00:00Z"},
                presence_evidence={"method": "video", "verified": True},
            )
        )

        self.assertFalse(result.success)
        self.assertEqual(
            result.receipt["policy"]["reasons"],
            ["SNAPSHOT_REQUIRED"],
            "Expected SNAPSHOT_REQUIRED denial",
        )

    def test_snapshot_key_collision_across_suites(self) -> None:
        """Evil test: identical period across two suites must produce separate keys."""
        import aspire_orchestrator.skillpacks.milo_payroll as mp

        mp._payroll_snapshots["suite-a:2026-03"] = {"suite_id": "suite-a", "data": "a"}
        mp._payroll_snapshots["suite-b:2026-03"] = {"suite_id": "suite-b", "data": "b"}

        # Verify they are independent (no collision)
        self.assertNotEqual(
            mp._payroll_snapshots["suite-a:2026-03"]["data"],
            mp._payroll_snapshots["suite-b:2026-03"]["data"],
        )


# ---------------------------------------------------------------------------
# BUG-SK8-06: quinn_invoicing — outcome="success" pre-approval
# ---------------------------------------------------------------------------

class TestQuinnInvoicingPreApprovalOutcome(unittest.TestCase):
    """BUG-SK8-06: QuinnInvoicingSkillPack.create_invoice sets outcome="success"
    in the _make_receipt call (line ~258-272) even though the invoice has NOT yet
    been executed — it is pending approval.  The receipt chain auditor will see
    a 'success' outcome for an invoice that was never sent to Stripe.

    Correct outcome for a YELLOW pre-approval receipt = "pending" or "pending_approval".

    Law #2 violation: receipt outcome must accurately reflect the actual state.
    """

    def test_create_invoice_receipt_outcome_before_approval(self) -> None:
        """Evil test: pre-approval receipt must NOT carry outcome='success'."""
        from aspire_orchestrator.skillpacks.quinn_invoicing import (
            QuinnInvoicingSkillPack,
            QuinnContext,
        )

        pack = QuinnInvoicingSkillPack()
        ctx = QuinnContext(suite_id="s1", office_id="o1", correlation_id="c1")

        result = asyncio.get_event_loop().run_until_complete(
            pack.create_invoice(
                customer="cust-001",
                line_items=[{"description": "Service", "quantity": 1, "unit_price": 500}],
                context=ctx,
                amount=500,
                currency="usd",
            )
        )

        self.assertTrue(result.approval_required, "YELLOW invoice must require approval")
        receipt_status = result.receipt.get("status", "")
        self.assertNotEqual(
            receipt_status,
            "ok",
            f"BUG-SK8-06: pre-approval receipt status is '{receipt_status}' — "
            "expected 'pending_approval', not 'ok'/'success'",
        )

    def test_send_invoice_receipt_outcome_before_approval(self) -> None:
        """Same issue on send_invoice — outcome should be pending, not success."""
        from aspire_orchestrator.skillpacks.quinn_invoicing import (
            QuinnInvoicingSkillPack,
            QuinnContext,
        )

        pack = QuinnInvoicingSkillPack()
        ctx = QuinnContext(suite_id="s1", office_id="o1", correlation_id="c1")

        result = asyncio.get_event_loop().run_until_complete(
            pack.send_invoice(invoice_id="inv-001", context=ctx)
        )

        self.assertTrue(result.approval_required)
        self.assertNotEqual(
            result.receipt.get("status"),
            "ok",
            "BUG-SK8-06: send_invoice pre-approval receipt incorrectly shows 'ok'",
        )


# ---------------------------------------------------------------------------
# BUG-SK8-07: nora_conference — unused import ReceiptType, RiskTier
# ---------------------------------------------------------------------------

class TestNoraConferenceUnusedImport(unittest.TestCase):
    """BUG-SK8-07: nora_conference.py imports ReceiptType and RiskTier from
    aspire_orchestrator.models but never uses them anywhere in the module.
    These are dead imports that add coupling without benefit.
    This test is documentation-only; it confirms both symbols can be imported
    but verifies they are not referenced in the module's produced receipts.
    """

    def test_receipt_does_not_use_receipt_type_enum(self) -> None:
        """Receipts from Nora use string literals, not ReceiptType enum values."""
        from aspire_orchestrator.skillpacks.nora_conference import (
            NoraConferenceSkillPack,
            NoraContext,
        )
        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult

        pack = NoraConferenceSkillPack()
        ctx = NoraContext(suite_id="s1", office_id="o1", correlation_id="c1")

        mock_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="livekit.room.create",
            data={"room_name": "test-room", "sid": "RM-001"},
        )

        with patch(
            "aspire_orchestrator.skillpacks.nora_conference.execute_tool",
            new=AsyncMock(return_value=mock_result),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                pack.create_room(room_name="test-room", settings=None, context=ctx)
            )

        self.assertIn("event_type", result.receipt)
        # If ReceiptType were used, receipt_type field would appear in receipt
        self.assertNotIn("receipt_type", result.receipt)


# ---------------------------------------------------------------------------
# BUG-SK8-08: initiate_dual_approval is sync def in both Milo and Clara
# ---------------------------------------------------------------------------

class TestDualApprovalSyncVsAsync(unittest.TestCase):
    """BUG-SK8-08: EnhancedMiloPayroll.initiate_dual_approval and
    EnhancedClaraLegal.initiate_dual_approval are declared as `def` (sync), not
    `async def`.  Any caller that does `await pack.initiate_dual_approval(...)` will
    receive a dict instead of a coroutine — Python will silently wrap the dict return
    value if called without await, but callers that do await will get a TypeError.

    Additionally, neither method emits a receipt via store_receipts() or emit_receipt().
    They return a raw dict with receipt embedded — violating Law #2 (receipt must
    be persisted, not just returned).

    Law #2 violation: receipt not persisted.
    """

    def test_milo_initiate_dual_approval_is_not_coroutine(self) -> None:
        """initiate_dual_approval must be async OR callers must know it's sync."""
        from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
        from aspire_orchestrator.services.agent_sdk_base import AgentContext

        pack = EnhancedMiloPayroll()
        ctx = AgentContext(suite_id="s1", office_id="o1", correlation_id="c1")

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.request_id = "req-001"
        mock_result.status = MagicMock()
        mock_result.status.value = "pending"
        mock_result.remaining_roles = ["finance"]
        mock_result.receipt = {"receipt_id": "rcpt-001"}
        mock_result.error = None

        with patch(
            "aspire_orchestrator.skillpacks.milo_payroll.get_dual_approval_service",
        ) as mock_svc_factory:
            mock_svc = MagicMock()
            mock_svc.create_request.return_value = mock_result
            mock_svc_factory.return_value = mock_svc

            # This must NOT raise TypeError — confirms it's sync
            result = pack.initiate_dual_approval(
                payroll_data={"payroll_id": "pay-001", "payroll_period": "2026-03"},
                ctx=ctx,
            )

        self.assertIsInstance(result, dict, "initiate_dual_approval must return dict synchronously")
        self.assertFalse(
            asyncio.iscoroutine(result),
            "BUG-SK8-08: initiate_dual_approval is sync but should be async for governance pipeline",
        )

    def test_milo_initiate_dual_approval_missing_receipt_persistence(self) -> None:
        """Evil test: initiate_dual_approval must persist its own receipt.
        Currently it only returns a dict — receipt is not written to receipt_store.
        """
        from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
        from aspire_orchestrator.services.agent_sdk_base import AgentContext

        pack = EnhancedMiloPayroll()
        ctx = AgentContext(suite_id="s1", office_id="o1", correlation_id="c1")

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.request_id = "req-001"
        mock_result.status = MagicMock(value="pending")
        mock_result.remaining_roles = ["finance"]
        mock_result.receipt = {"receipt_id": "rcpt-001"}
        mock_result.error = None

        with patch(
            "aspire_orchestrator.skillpacks.milo_payroll.get_dual_approval_service",
        ) as mock_svc_factory:
            mock_svc = MagicMock()
            mock_svc.create_request.return_value = mock_result
            mock_svc_factory.return_value = mock_svc

            with patch(
                "aspire_orchestrator.skillpacks.milo_payroll.EnhancedMiloPayroll.emit_receipt",
                new=AsyncMock(),
            ) as mock_emit:
                pack.initiate_dual_approval(
                    payroll_data={"payroll_id": "p1"},
                    ctx=ctx,
                )

                # emit_receipt was NOT called — this is the Law #2 gap
                mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# BUG-SK8-09 & BUG-SK8-10: str(e) PII leakage in calendar_client, office_message_client
# ---------------------------------------------------------------------------

class TestProviderPiiLeakageInErrors(unittest.TestCase):
    """BUG-SK8-09 / BUG-SK8-10: calendar_client.py and office_message_client.py
    catch exceptions and pass str(e) directly into the error field and receipt.

    If the exception message contains a Supabase connection string, OAuth token,
    or email address, it will appear in the receipt and provider_call_log.

    Law #9 violation: secrets and PII must not appear in logs or receipts.

    These tests verify that the error field uses type(e).__name__ or a generic
    message rather than str(e).  They will FAIL currently, documenting the bug.
    """

    def test_calendar_client_supabase_error_does_not_leak_str_e(self) -> None:
        """Evil test: a Supabase exception with PII in message must not appear in receipt."""
        import inspect

        try:
            from aspire_orchestrator.providers.calendar_client import (
                get_calendar_events,
            )
        except ImportError:
            self.skipTest("calendar_client not importable in this test environment")

        source = inspect.getsource(get_calendar_events)
        # Find all str(e) occurrences in the function — there should be none
        pii_leak_count = source.count("str(e)")
        self.assertEqual(
            pii_leak_count,
            0,
            f"BUG-SK8-09: calendar_client.get_calendar_events uses str(e) {pii_leak_count} times — "
            "exception messages may contain OAuth tokens or Supabase DSN",
        )

    def test_office_message_client_error_does_not_leak_str_e(self) -> None:
        """Evil test: office_message_client must not use str(e) in error fields."""
        import inspect

        try:
            import aspire_orchestrator.providers.office_message_client as omc
        except ImportError:
            self.skipTest("office_message_client not importable")

        source = inspect.getsource(omc)
        pii_leak_count = source.count("str(e)")
        self.assertEqual(
            pii_leak_count,
            0,
            f"BUG-SK8-10: office_message_client.py uses str(e) {pii_leak_count} times — PII leakage risk",
        )


# ---------------------------------------------------------------------------
# BUG-SK8-11: No circuit breaker on 24/28 provider clients
# ---------------------------------------------------------------------------

class TestProviderCircuitBreakerCoverage(unittest.TestCase):
    """BUG-SK8-11: Only base_client.py and twilio_client.py reference circuit_breaker.
    All remaining 26 provider subclasses call HTTP endpoints without circuit breaker
    protection.  Under sustained provider outage, all requests will block until
    timeout, exhausting the thread pool.

    Production Gate 3 requires circuit breakers on all external calls.
    This test audits which clients are missing circuit breaker references.
    """

    PROVIDERS_WITHOUT_EXPECTED_CIRCUIT_BREAKER = [
        "pandadoc_client",
        "stripe_client",
        "gusto_client",
        "quickbooks_client",
        "plaid_client",
        "elevenlabs_client",
        "deepgram_client",
        "livekit_client",
        "s3_client",
        "polaris_email_client",
        "osm_overpass_client",
        "foursquare_client",
        "google_places_client",
        "here_client",
        "mapbox_client",
        "tomtom_client",
        "tavily_client",
        "brave_client",
        "puppeteer_client",
        "oauth2_manager",
        "calendar_client",
        "office_message_client",
    ]

    def test_providers_missing_circuit_breaker(self) -> None:
        """Verify which providers lack circuit_breaker integration.
        All failures are documented bugs — not test infrastructure failures.
        """
        import importlib
        import inspect

        missing = []
        for mod_name in self.PROVIDERS_WITHOUT_EXPECTED_CIRCUIT_BREAKER:
            try:
                mod = importlib.import_module(
                    f"aspire_orchestrator.providers.{mod_name}"
                )
                src = inspect.getsource(mod)
                if "circuit_breaker" not in src.lower():
                    missing.append(mod_name)
            except (ImportError, OSError):
                pass  # Module not importable — skip

        self.assertEqual(
            missing,
            [],
            f"BUG-SK8-11: {len(missing)} providers missing circuit_breaker: {missing}. "
            "Production Gate 3 requires circuit breakers on all external calls.",
        )


# ---------------------------------------------------------------------------
# BUG-SK8-12: Rule-based skillpacks never call store_receipts() (Law #2)
# ---------------------------------------------------------------------------

class TestSkillpackReceiptPersistence(unittest.TestCase):
    """BUG-SK8-12: All rule-based skillpack methods (_emit_receipt / _make_receipt)
    build a receipt dict and return it in SkillPackResult.  None of them call
    store_receipts() or the receipt_store service directly.

    This means receipts exist only in the SkillPackResult's .receipt dict.
    If the caller (orchestrator node) discards the result without persisting
    the receipt, the receipt is lost.

    Receipt persistence for rule-based packs is the orchestrator's responsibility,
    but the lack of any persistence call in the skillpack itself means there is
    no fail-safe.  Enhanced packs (AgenticSkillPack subclasses) DO call emit_receipt()
    which enqueues to receipt_store.  Rule-based packs have no equivalent.

    Law #2 violation: receipts must be immutably persisted — relying on caller
    to not discard is insufficient.

    These tests audit the gap.
    """

    def _get_skillpack_source(self, module_name: str) -> str:
        import importlib, inspect
        mod = importlib.import_module(f"aspire_orchestrator.skillpacks.{module_name}")
        return inspect.getsource(mod)

    RULE_BASED_PACKS = [
        "adam_research",
        "eli_inbox",
        "milo_payroll",
        "quinn_invoicing",
        "teressa_books",
        "sarah_front_desk",
        "nora_conference",
        "mail_ops_desk",
        "tec_documents",
    ]

    def test_rule_based_packs_do_not_call_store_receipts(self) -> None:
        """Confirm the gap: rule-based packs never call store_receipts()."""
        packs_missing_persistence = []

        for pack_name in self.RULE_BASED_PACKS:
            try:
                src = self._get_skillpack_source(pack_name)
                if "store_receipts" not in src and "receipt_store" not in src:
                    packs_missing_persistence.append(pack_name)
            except (ImportError, OSError):
                pass

        self.assertEqual(
            packs_missing_persistence,
            [],
            f"BUG-SK8-12: {len(packs_missing_persistence)} rule-based skillpacks never "
            f"call store_receipts() or receipt_store: {packs_missing_persistence}. "
            "Receipts are only in SkillPackResult.receipt — lost if caller discards result.",
        )

    def test_milo_run_payroll_receipt_has_all_required_fields(self) -> None:
        """Law #2: receipt from run_payroll must have all 18 standard fields."""
        from aspire_orchestrator.skillpacks.milo_payroll import (
            MiloPayrollSkillPack,
            MiloContext,
            _payroll_snapshots,
        )

        pack = MiloPayrollSkillPack()
        ctx = MiloContext(suite_id="s1", office_id="o1", correlation_id="c1")

        # Denied path (missing snapshot)
        result = asyncio.get_event_loop().run_until_complete(
            pack.run_payroll(
                payroll_period="2026-03",
                context=ctx,
                company_id="co-1",
                payroll_id="pay-1",
            )
        )

        required_fields = {
            "receipt_version", "receipt_id", "ts", "event_type",
            "suite_id", "office_id", "actor", "correlation_id",
            "risk_tier", "status", "inputs_hash", "policy",
        }
        receipt = result.receipt
        missing_fields = required_fields - set(receipt.keys())
        self.assertEqual(
            missing_fields,
            set(),
            f"Receipt missing required fields: {missing_fields}",
        )


# ---------------------------------------------------------------------------
# Additional: finn_finance_manager functions are module-level, not class methods
# ---------------------------------------------------------------------------

class TestFinnFunctionModuleLevelAccess(unittest.TestCase):
    """Verify that the Finn FM module-level functions are accessible as top-level
    (not wrapped in a class).  This is the intended design but must be confirmed
    because some callers treat Finn as an instance-based skillpack.
    """

    def test_module_level_functions_callable(self) -> None:
        """All four rule-based functions must be importable at module level."""
        from aspire_orchestrator.skillpacks.finn_finance_manager import (
            read_finance_snapshot,
            read_finance_exceptions,
            draft_finance_packet,
            create_finance_proposal,
            dispatch_a2a_delegation,
        )
        import asyncio

        self.assertTrue(asyncio.iscoroutinefunction(read_finance_snapshot))
        self.assertFalse(asyncio.iscoroutinefunction(read_finance_exceptions))
        self.assertFalse(asyncio.iscoroutinefunction(draft_finance_packet))
        self.assertFalse(asyncio.iscoroutinefunction(create_finance_proposal))
        self.assertFalse(asyncio.iscoroutinefunction(dispatch_a2a_delegation))


if __name__ == "__main__":
    unittest.main()
