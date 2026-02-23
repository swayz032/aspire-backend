"""QA Loop Service — Verify Before Respond (Phase 2 Brain Layer).

The QA loop is the LAST governance gate before a response reaches the user.
It runs AFTER execution and receipt generation, verifying that all governance
invariants were honored during the pipeline execution.

Per CLAUDE.md:
  - Law #2: QA loop itself generates a meta-receipt for its verification
  - Law #3: If QA finds a critical violation, it fails closed — no unverified response
  - Law #4: Risk tier governance must be honored (YELLOW/RED require approval)
  - Law #5: Capability tokens must be used for tool execution
  - Law #9: No PII leakage in receipts

Checks performed (7 total):
  1. receipt_exists — at least one receipt generated for this execution
  2. receipt_has_required_fields — structural completeness
  3. risk_tier_honored — YELLOW/RED actions have approval evidence
  4. approval_evidence_valid — approver_id and approval_method present
  5. outcome_consistent — receipt outcome matches execution result
  6. no_pii_leak — no obvious PII patterns in redacted fields
  7. capability_token_used — tool execution has capability_token_id

Retry logic: if retry_suggested and retry_count < max_retries, the orchestrator
can retry the execution. Critical violations that cannot be retried trigger
escalation.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from aspire_orchestrator.models import Outcome, RiskTier

logger = logging.getLogger(__name__)

# PII patterns for lightweight detection (no Presidio dependency)
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# Receipt fields that must be present per CLAUDE.md Law #2
_REQUIRED_RECEIPT_FIELDS = frozenset({
    "correlation_id",
    "suite_id",
    "action_type",
    "outcome",
    "receipt_hash",
})

# Default max retries
_DEFAULT_MAX_RETRIES = 1


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class QAViolation(BaseModel):
    """A single QA check failure."""

    check_name: str
    severity: str = Field(pattern=r"^(critical|warning)$")
    message: str
    evidence: dict[str, Any] | None = None


class QAResult(BaseModel):
    """Aggregate result of all QA checks."""

    passed: bool
    violations: list[QAViolation] = Field(default_factory=list)
    retry_suggested: bool = False
    retry_count: int = 0
    max_retries: int = _DEFAULT_MAX_RETRIES
    escalation_required: bool = False


# ---------------------------------------------------------------------------
# QA Loop
# ---------------------------------------------------------------------------


class QALoop:
    """Governance verification gate — runs after execution, before respond.

    Each check is a separate private method for independent testability.
    All checks run to completion (no short-circuit) so the full violation
    set is available for debugging and escalation.
    """

    def verify(self, state: dict[str, Any]) -> QAResult:
        """Run all governance checks against the pipeline state.

        Args:
            state: LangGraph state dict containing receipts, approval_evidence,
                   risk_tier, action_type, outcome, capability_token, etc.

        Returns:
            QAResult with pass/fail status and any violations found.
        """
        violations: list[QAViolation] = []
        receipts = state.get("receipts", [])
        retry_count = state.get("qa_retry_count", 0)

        # Run all checks — collect every violation
        violations.extend(self._check_receipt_exists(receipts))
        violations.extend(self._check_receipt_has_required_fields(receipts))
        violations.extend(self._check_risk_tier_honored(state, receipts))
        violations.extend(self._check_approval_evidence_valid(state, receipts))
        violations.extend(self._check_outcome_consistent(state, receipts))
        violations.extend(self._check_no_pii_leak(receipts))
        violations.extend(self._check_capability_token_used(state, receipts))

        has_critical = any(v.severity == "critical" for v in violations)
        has_warning = any(v.severity == "warning" for v in violations)

        # Retry is only suggested for critical violations that might be
        # transient (e.g., receipt write failure) and only if under limit
        retry_suggested = has_critical and retry_count < _DEFAULT_MAX_RETRIES

        # Escalation when critical violations persist beyond retry capacity
        escalation_required = has_critical and retry_count >= _DEFAULT_MAX_RETRIES

        passed = len(violations) == 0

        if passed:
            logger.info(
                "QA loop PASSED: action=%s, receipts=%d",
                state.get("action_type", "unknown"),
                len(receipts),
            )
        else:
            for v in violations:
                logger.warning(
                    "QA VIOLATION: check=%s, severity=%s, message=%s",
                    v.check_name, v.severity, v.message,
                )
            logger.warning(
                "QA loop FAILED: action=%s, violations=%d (critical=%d, warning=%d), "
                "retry_suggested=%s, escalation=%s",
                state.get("action_type", "unknown"),
                len(violations),
                sum(1 for v in violations if v.severity == "critical"),
                sum(1 for v in violations if v.severity == "warning"),
                retry_suggested,
                escalation_required,
            )

        return QAResult(
            passed=passed,
            violations=violations,
            retry_suggested=retry_suggested,
            retry_count=retry_count,
            max_retries=_DEFAULT_MAX_RETRIES,
            escalation_required=escalation_required,
        )

    def build_meta_receipt(
        self,
        state: dict[str, Any],
        qa_result: QAResult,
    ) -> dict[str, Any]:
        """Build a meta-receipt for the QA verification itself (Law #2).

        Every governance gate produces a receipt — including the QA loop.
        """
        now = datetime.now(timezone.utc).isoformat()
        violation_summaries = [
            {"check": v.check_name, "severity": v.severity, "message": v.message}
            for v in qa_result.violations
        ]

        return {
            "id": str(uuid4()),
            "correlation_id": state.get("correlation_id", str(uuid4())),
            "suite_id": state.get("suite_id", "unknown"),
            "office_id": state.get("office_id", "unknown"),
            "actor_type": "system",
            "actor_id": "qa_loop",
            "action_type": "qa.verify",
            "risk_tier": "green",
            "tool_used": "qa_loop",
            "outcome": "success" if qa_result.passed else "failed",
            "reason_code": None if qa_result.passed else "qa_violations_found",
            "created_at": now,
            "receipt_type": "qa_verification",
            "redacted_inputs": None,
            "redacted_outputs": {
                "passed": qa_result.passed,
                "violation_count": len(qa_result.violations),
                "violations": violation_summaries,
                "retry_suggested": qa_result.retry_suggested,
                "escalation_required": qa_result.escalation_required,
            },
        }

    # ------------------------------------------------------------------
    # Individual checks (private, independently testable)
    # ------------------------------------------------------------------

    def _check_receipt_exists(
        self,
        receipts: list[dict[str, Any]],
    ) -> list[QAViolation]:
        """Check 1: At least one receipt was generated for this execution."""
        if not receipts:
            return [QAViolation(
                check_name="receipt_exists",
                severity="critical",
                message="No receipts generated for this execution (Law #2 violation)",
                evidence={"receipt_count": 0},
            )]
        return []

    def _check_receipt_has_required_fields(
        self,
        receipts: list[dict[str, Any]],
    ) -> list[QAViolation]:
        """Check 2: All receipts have required structural fields."""
        violations: list[QAViolation] = []
        for i, receipt in enumerate(receipts):
            missing = [
                f for f in _REQUIRED_RECEIPT_FIELDS
                if not receipt.get(f)
            ]
            if missing:
                violations.append(QAViolation(
                    check_name="receipt_has_required_fields",
                    severity="critical",
                    message=f"Receipt {i} missing required fields: {', '.join(sorted(missing))}",
                    evidence={
                        "receipt_index": i,
                        "receipt_id": receipt.get("id", "unknown"),
                        "missing_fields": sorted(missing),
                    },
                ))
        return violations

    def _check_risk_tier_honored(
        self,
        state: dict[str, Any],
        receipts: list[dict[str, Any]],
    ) -> list[QAViolation]:
        """Check 3: YELLOW/RED actions have approval evidence in state."""
        risk_tier = state.get("risk_tier", "")
        if isinstance(risk_tier, RiskTier):
            risk_tier = risk_tier.value

        if risk_tier not in ("yellow", "red"):
            return []

        approval_evidence = state.get("approval_evidence")
        if not approval_evidence:
            return [QAViolation(
                check_name="risk_tier_honored",
                severity="critical",
                message=(
                    f"Action has risk tier {risk_tier.upper()} but no approval "
                    f"evidence found in state (Law #4 violation)"
                ),
                evidence={
                    "risk_tier": risk_tier,
                    "action_type": state.get("action_type", "unknown"),
                    "approval_evidence": None,
                },
            )]
        return []

    def _check_approval_evidence_valid(
        self,
        state: dict[str, Any],
        receipts: list[dict[str, Any]],
    ) -> list[QAViolation]:
        """Check 4: If approval evidence exists, it has required fields."""
        risk_tier = state.get("risk_tier", "")
        if isinstance(risk_tier, RiskTier):
            risk_tier = risk_tier.value

        if risk_tier not in ("yellow", "red"):
            return []

        approval_evidence = state.get("approval_evidence")
        if not approval_evidence:
            # Already caught by check 3
            return []

        violations: list[QAViolation] = []

        # Accept both dict and Pydantic model
        if isinstance(approval_evidence, dict):
            approver_id = approval_evidence.get("approver_id")
            approval_method = approval_evidence.get("approval_method")
        else:
            approver_id = getattr(approval_evidence, "approver_id", None)
            approval_method = getattr(approval_evidence, "approval_method", None)

        if not approver_id:
            violations.append(QAViolation(
                check_name="approval_evidence_valid",
                severity="critical",
                message="Approval evidence missing approver_id",
                evidence={"approval_evidence": str(approval_evidence)},
            ))

        if not approval_method:
            violations.append(QAViolation(
                check_name="approval_evidence_valid",
                severity="critical",
                message="Approval evidence missing approval_method",
                evidence={"approval_evidence": str(approval_evidence)},
            ))

        return violations

    def _check_outcome_consistent(
        self,
        state: dict[str, Any],
        receipts: list[dict[str, Any]],
    ) -> list[QAViolation]:
        """Check 5: Receipt outcome is consistent with execution result."""
        if not receipts:
            # Already caught by check 1
            return []

        state_outcome = state.get("outcome", "")
        if isinstance(state_outcome, Outcome):
            state_outcome = state_outcome.value

        if not state_outcome:
            return []

        violations: list[QAViolation] = []

        # Check the last receipt (the execution outcome receipt)
        last_receipt = receipts[-1]
        receipt_outcome = last_receipt.get("outcome", "")
        if isinstance(receipt_outcome, Outcome):
            receipt_outcome = receipt_outcome.value

        if state_outcome == "success" and receipt_outcome == "denied":
            violations.append(QAViolation(
                check_name="outcome_consistent",
                severity="critical",
                message=(
                    "State outcome is 'success' but last receipt outcome is "
                    "'denied' — execution and receipt are inconsistent"
                ),
                evidence={
                    "state_outcome": state_outcome,
                    "receipt_outcome": receipt_outcome,
                    "receipt_id": last_receipt.get("id", "unknown"),
                },
            ))

        if state_outcome == "denied" and receipt_outcome == "success":
            violations.append(QAViolation(
                check_name="outcome_consistent",
                severity="critical",
                message=(
                    "State outcome is 'denied' but last receipt outcome is "
                    "'success' — execution and receipt are inconsistent"
                ),
                evidence={
                    "state_outcome": state_outcome,
                    "receipt_outcome": receipt_outcome,
                    "receipt_id": last_receipt.get("id", "unknown"),
                },
            ))

        return violations

    def _check_no_pii_leak(
        self,
        receipts: list[dict[str, Any]],
    ) -> list[QAViolation]:
        """Check 6: No obvious PII patterns in receipt redacted fields.

        Uses lightweight regex patterns — NOT a replacement for Presidio DLP,
        but a fast sanity check for SSN and credit card numbers that should
        have been redacted before reaching the QA loop.
        """
        violations: list[QAViolation] = []

        for i, receipt in enumerate(receipts):
            for field_name in ("redacted_inputs", "redacted_outputs"):
                field_value = receipt.get(field_name)
                if not field_value:
                    continue

                text = str(field_value)

                if _SSN_PATTERN.search(text):
                    violations.append(QAViolation(
                        check_name="no_pii_leak",
                        severity="warning",
                        message=(
                            f"Receipt {i} field '{field_name}' may contain "
                            f"an unredacted SSN pattern (Law #9)"
                        ),
                        evidence={
                            "receipt_index": i,
                            "receipt_id": receipt.get("id", "unknown"),
                            "field": field_name,
                            "pattern": "SSN",
                        },
                    ))

                if _CC_PATTERN.search(text):
                    violations.append(QAViolation(
                        check_name="no_pii_leak",
                        severity="warning",
                        message=(
                            f"Receipt {i} field '{field_name}' may contain "
                            f"an unredacted credit card number (Law #9)"
                        ),
                        evidence={
                            "receipt_index": i,
                            "receipt_id": receipt.get("id", "unknown"),
                            "field": field_name,
                            "pattern": "CREDIT_CARD",
                        },
                    ))

        return violations

    def _check_capability_token_used(
        self,
        state: dict[str, Any],
        receipts: list[dict[str, Any]],
    ) -> list[QAViolation]:
        """Check 7: Tool execution receipts have capability_token_id.

        Only applies when a tool was actually executed (not for denied/failed
        flows that never reached execution).
        """
        state_outcome = state.get("outcome", "")
        if isinstance(state_outcome, Outcome):
            state_outcome = state_outcome.value

        # If the flow was denied before execution, no token is expected
        if state_outcome in ("denied", "failed"):
            return []

        tool_used = state.get("tool_used", "")
        if not tool_used:
            return []

        violations: list[QAViolation] = []

        # Find actual tool execution receipts (receipt_type == "tool_execution")
        # Internal orchestrator receipts (intake, policy, approval) also set
        # tool_used but are NOT tool executions and don't need capability tokens
        execution_receipts = [
            r for r in receipts
            if r.get("receipt_type") == "tool_execution"
            and r.get("outcome") == "success"
        ]

        for receipt in execution_receipts:
            if not receipt.get("capability_token_id"):
                violations.append(QAViolation(
                    check_name="capability_token_used",
                    severity="critical",
                    message=(
                        f"Tool execution receipt missing capability_token_id "
                        f"(Law #5 violation)"
                    ),
                    evidence={
                        "receipt_id": receipt.get("id", "unknown"),
                        "tool_used": receipt.get("tool_used", "unknown"),
                    },
                ))

        return violations
