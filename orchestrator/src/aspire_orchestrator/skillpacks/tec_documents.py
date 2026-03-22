"""Tec Documents Skill Pack — PDF generation, preview, and sharing.

Tec is the Documents desk. He handles:
  - Document generation (PDF from templates via Puppeteer)
  - Document preview (presigned S3 URLs)
  - Document sharing (external communication — YELLOW tier)

Provider integration:
  - puppeteer.pdf.generate: Headless Chrome PDF rendering
  - s3.document.upload: Store generated documents
  - s3.url.sign: Generate presigned URLs for preview/download

Risk tiers:
  - document.generate: GREEN (internal, non-destructive)
  - document.preview: GREEN (read-only presigned URL)
  - document.share: YELLOW (external communication, requires approval)

Law compliance:
  - Law #1: Skill pack proposes; orchestrator decides
  - Law #2: Every action emits a receipt (success AND failure)
  - Law #3: Fail closed on missing parameters or tool failures
  - Law #4: GREEN for generate/preview, YELLOW for share
  - Law #5: Capability tokens validated by tool executors
  - Law #7: Tools execute bounded commands only
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome, RiskTier
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

ACTOR_TEC = "skillpack:tec-documents"
RECEIPT_VERSION = "1.0"

VALID_TEMPLATE_TYPES = frozenset({
    "invoice",
    "proposal",
    "contract",
    "report",
    "letter",
})

# S3 bucket for document storage
DEFAULT_BUCKET = "aspire-documents"

# Presigned URL default expiry (1 hour)
DEFAULT_URL_EXPIRY_SECONDS = 3600


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True)
class SkillPackResult:
    """Result from a Tec Documents skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class TecContext:
    """Required context for all Tec operations."""

    suite_id: str
    office_id: str
    correlation_id: str
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


# =============================================================================
# Receipt Helpers
# =============================================================================


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """Compute SHA256 hash of inputs for receipt linkage."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _make_receipt(
    *,
    ctx: TecContext,
    event_type: str,
    status: str,
    inputs: dict[str, Any],
    risk_tier: str = "green",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Tec Documents operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_TEC,
        "correlation_id": ctx.correlation_id,
        "status": status,
        "risk_tier": risk_tier,
        "inputs_hash": _compute_inputs_hash(inputs),
    }
    if metadata:
        receipt["metadata"] = metadata
    return receipt


# =============================================================================
# Skill Pack
# =============================================================================


class TecDocumentsSkillPack:
    async def document_generate(
        self,
        template_id: str,
        data: dict[str, Any],
        context: TecContext,
        *,
        execute_tool_fn: Any | None = None,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.generate_document(
            template_id=template_id,
            data=data,
            context=context,
            execute_tool_fn=execute_tool_fn,
        )

    async def document_preview(
        self,
        document_id: str,
        context: TecContext,
        *,
        execute_tool_fn: Any | None = None,
        expires_in: int = DEFAULT_URL_EXPIRY_SECONDS,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.preview_document(
            document_id=document_id,
            context=context,
            execute_tool_fn=execute_tool_fn,
            expires_in=expires_in,
        )

    async def document_share(
        self,
        document_id: str,
        recipients: list[str],
        context: TecContext,
        *,
        execute_tool_fn: Any | None = None,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.share_document(
            document_id=document_id,
            recipients=recipients,
            context=context,
            execute_tool_fn=execute_tool_fn,
        )

    """Tec Documents skill pack — PDF generation, preview, sharing.

    All methods are async to match the tool executor interface.
    Each method validates inputs, calls tool executors, and emits receipts.
    """

    async def generate_document(
        self,
        template_id: str,
        data: dict[str, Any],
        context: TecContext,
        *,
        execute_tool_fn: Any | None = None,
    ) -> SkillPackResult:
        """Generate a document from a template (GREEN tier).

        Steps:
          1. Validate template_id against allowed types
          2. Generate PDF via puppeteer.pdf.generate
          3. Upload to S3 via s3.document.upload
          4. Emit receipt

        Args:
            template_id: Template type (invoice, proposal, contract, report, letter)
            data: Template data (merged into HTML)
            context: Tenant context (suite_id, office_id, correlation_id)
            execute_tool_fn: Optional tool executor (for testing/DI)
        """
        # Validate template_id
        if not template_id:
            receipt = _make_receipt(
                ctx=context,
                event_type="document.generate",
                status="denied",
                inputs={"template_id": "", "action": "document.generate"},
                metadata={"reason": "MISSING_TEMPLATE_ID"},
            )
            return SkillPackResult(
                success=False,
                error="Missing required parameter: template_id",
                receipt=receipt,
            )

        if template_id not in VALID_TEMPLATE_TYPES:
            receipt = _make_receipt(
                ctx=context,
                event_type="document.generate",
                status="denied",
                inputs={"template_id": template_id, "action": "document.generate"},
                metadata={"reason": "INVALID_TEMPLATE_TYPE"},
            )
            return SkillPackResult(
                success=False,
                error=f"Invalid template_id '{template_id}'. "
                      f"Allowed: {sorted(VALID_TEMPLATE_TYPES)}",
                receipt=receipt,
            )

        # Build HTML from template data
        html = self._render_template(template_id, data)

        # Step 1: Generate PDF
        pdf_result = await self._execute_tool(
            execute_tool_fn,
            tool_id="puppeteer.pdf.generate",
            payload={"html": html, "options": {"format": "A4"}},
            context=context,
            risk_tier="green",
        )

        if pdf_result.outcome != Outcome.SUCCESS:
            receipt = _make_receipt(
                ctx=context,
                event_type="document.generate",
                status="failed",
                inputs={"template_id": template_id, "action": "document.generate"},
                metadata={"reason": pdf_result.error or "PDF_GENERATION_FAILED"},
            )
            return SkillPackResult(
                success=False,
                error=pdf_result.error or "PDF generation failed",
                receipt=receipt,
            )

        # Step 2: Upload to S3
        document_key = (
            f"{context.suite_id}/{template_id}/"
            f"{context.correlation_id}.pdf"
        )

        upload_result = await self._execute_tool(
            execute_tool_fn,
            tool_id="s3.document.upload",
            payload={
                "bucket": DEFAULT_BUCKET,
                "key": document_key,
                "content_type": "application/pdf",
            },
            context=context,
            risk_tier="green",
        )

        if upload_result.outcome != Outcome.SUCCESS:
            receipt = _make_receipt(
                ctx=context,
                event_type="document.generate",
                status="failed",
                inputs={"template_id": template_id, "action": "document.generate"},
                metadata={"reason": upload_result.error or "S3_UPLOAD_FAILED"},
            )
            return SkillPackResult(
                success=False,
                error=upload_result.error or "Document upload failed",
                receipt=receipt,
            )

        # Success
        receipt = _make_receipt(
            ctx=context,
            event_type="document.generate",
            status="ok",
            inputs={"template_id": template_id, "action": "document.generate"},
            metadata={
                "document_key": document_key,
                "bucket": DEFAULT_BUCKET,
                "template_id": template_id,
            },
        )

        return SkillPackResult(
            success=True,
            data={
                "document_id": document_key,
                "bucket": DEFAULT_BUCKET,
                "key": document_key,
                "template_id": template_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            receipt=receipt,
        )

    async def preview_document(
        self,
        document_id: str,
        context: TecContext,
        *,
        execute_tool_fn: Any | None = None,
        expires_in: int = DEFAULT_URL_EXPIRY_SECONDS,
    ) -> SkillPackResult:
        """Generate a presigned URL for document preview (GREEN tier).

        Args:
            document_id: S3 key of the document
            context: Tenant context
            execute_tool_fn: Optional tool executor (for testing/DI)
            expires_in: URL expiry in seconds (default 3600)
        """
        if not document_id:
            receipt = _make_receipt(
                ctx=context,
                event_type="document.preview",
                status="denied",
                inputs={"document_id": "", "action": "document.preview"},
                metadata={"reason": "MISSING_DOCUMENT_ID"},
            )
            return SkillPackResult(
                success=False,
                error="Missing required parameter: document_id",
                receipt=receipt,
            )

        # Tenant isolation check: document_id must start with suite_id
        if not document_id.startswith(f"{context.suite_id}/"):
            receipt = _make_receipt(
                ctx=context,
                event_type="document.preview",
                status="denied",
                inputs={"document_id": document_id, "action": "document.preview"},
                metadata={"reason": "TENANT_ISOLATION_VIOLATION"},
            )
            return SkillPackResult(
                success=False,
                error="Document does not belong to this tenant (Law #6)",
                receipt=receipt,
            )

        sign_result = await self._execute_tool(
            execute_tool_fn,
            tool_id="s3.url.sign",
            payload={
                "bucket": DEFAULT_BUCKET,
                "key": document_id,
                "expires_in": expires_in,
            },
            context=context,
            risk_tier="green",
        )

        if sign_result.outcome != Outcome.SUCCESS:
            receipt = _make_receipt(
                ctx=context,
                event_type="document.preview",
                status="failed",
                inputs={"document_id": document_id, "action": "document.preview"},
                metadata={"reason": sign_result.error or "URL_SIGN_FAILED"},
            )
            return SkillPackResult(
                success=False,
                error=sign_result.error or "Failed to generate presigned URL",
                receipt=receipt,
            )

        receipt = _make_receipt(
            ctx=context,
            event_type="document.preview",
            status="ok",
            inputs={"document_id": document_id, "action": "document.preview"},
            metadata={
                "document_id": document_id,
                "expires_in": expires_in,
            },
        )

        return SkillPackResult(
            success=True,
            data={
                "presigned_url": sign_result.data.get("presigned_url", ""),
                "document_id": document_id,
                "expires_in": expires_in,
            },
            receipt=receipt,
        )

    async def share_document(
        self,
        document_id: str,
        recipients: list[str],
        context: TecContext,
        *,
        execute_tool_fn: Any | None = None,
    ) -> SkillPackResult:
        """Share a document with external recipients (YELLOW tier).

        This is a YELLOW tier operation because it involves external
        communication. Returns approval_required=True — the orchestrator
        must obtain user confirmation before actually sending.

        Args:
            document_id: S3 key of the document to share
            recipients: List of email addresses to share with
            context: Tenant context
            execute_tool_fn: Optional tool executor (for testing/DI)
        """
        if not document_id:
            receipt = _make_receipt(
                ctx=context,
                event_type="document.share",
                status="denied",
                risk_tier="yellow",
                inputs={"document_id": "", "action": "document.share"},
                metadata={"reason": "MISSING_DOCUMENT_ID"},
            )
            return SkillPackResult(
                success=False,
                error="Missing required parameter: document_id",
                receipt=receipt,
            )

        if not recipients:
            receipt = _make_receipt(
                ctx=context,
                event_type="document.share",
                status="denied",
                risk_tier="yellow",
                inputs={"document_id": document_id, "action": "document.share"},
                metadata={"reason": "MISSING_RECIPIENTS"},
            )
            return SkillPackResult(
                success=False,
                error="Missing required parameter: recipients",
                receipt=receipt,
            )

        # Tenant isolation check
        if not document_id.startswith(f"{context.suite_id}/"):
            receipt = _make_receipt(
                ctx=context,
                event_type="document.share",
                status="denied",
                risk_tier="yellow",
                inputs={"document_id": document_id, "action": "document.share"},
                metadata={"reason": "TENANT_ISOLATION_VIOLATION"},
            )
            return SkillPackResult(
                success=False,
                error="Document does not belong to this tenant (Law #6)",
                receipt=receipt,
            )

        # YELLOW tier: return approval_required=True
        # The orchestrator handles the approval flow before execution
        receipt = _make_receipt(
            ctx=context,
            event_type="document.share",
            status="approval_required",
            risk_tier="yellow",
            inputs={
                "document_id": document_id,
                "recipients": recipients,
                "action": "document.share",
            },
            metadata={
                "document_id": document_id,
                "recipient_count": len(recipients),
                "approval_required": True,
            },
        )

        return SkillPackResult(
            success=True,
            data={
                "document_id": document_id,
                "recipients": recipients,
                "approval_required": True,
                "risk_tier": "yellow",
            },
            receipt=receipt,
        )

    # ─────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────

    def _render_template(self, template_id: str, data: dict[str, Any]) -> str:
        """Render a template to HTML.

        In production, this would use a template engine (Jinja2, Handlebars).
        For Phase 2, returns a minimal HTML document with the data embedded.
        """
        title = data.get("title", f"{template_id.title()} Document")
        body_content = data.get("body", "")

        return (
            f"<!DOCTYPE html><html><head><title>{title}</title></head>"
            f"<body><h1>{title}</h1>"
            f"<div class='content'>{body_content}</div>"
            f"<footer>Generated by Aspire Tec Documents — {template_id}</footer>"
            f"</body></html>"
        )

    @staticmethod
    async def _execute_tool(
        execute_tool_fn: Any | None,
        *,
        tool_id: str,
        payload: dict[str, Any],
        context: TecContext,
        risk_tier: str,
    ) -> ToolExecutionResult:
        """Execute a tool via the injected executor or the real registry.

        Uses dependency injection for testability. If no executor is
        provided, imports and calls the real tool_executor.execute_tool.
        """
        if execute_tool_fn is not None:
            return await execute_tool_fn(
                tool_id=tool_id,
                payload=payload,
                correlation_id=context.correlation_id,
                suite_id=context.suite_id,
                office_id=context.office_id,
                risk_tier=risk_tier,
                capability_token_id=context.capability_token_id,
                capability_token_hash=context.capability_token_hash,
            )

        # Real execution path
        from aspire_orchestrator.services.tool_executor import execute_tool

        return await execute_tool(
            tool_id=tool_id,
            payload=payload,
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier=risk_tier,
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )


# =============================================================================
# Module-level singleton
# =============================================================================

_skillpack: TecDocumentsSkillPack | None = None


def get_tec_documents_skillpack() -> TecDocumentsSkillPack:
    """Get the singleton Tec Documents skill pack instance."""
    global _skillpack
    if _skillpack is None:
        _skillpack = TecDocumentsSkillPack()
    return _skillpack


# =============================================================================
# Phase 3 W3: Enhanced Tec Documents with LLM reasoning
# =============================================================================

from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class EnhancedTecDocuments(AgenticSkillPack):
    """LLM-enhanced Tec Documents — document planning, intelligent drafting.

    Extends TecDocumentsSkillPack with:
    - plan_document: LLM plans document structure before generation
    - draft_content: LLM generates document content from outline
    - review_document: LLM reviews draft for completeness and tone

    Model routing:
    - plan_document: cheap_classifier (GPT-5-mini) for structure planning
    - draft_content: primary_reasoner (GPT-5.2) for content generation
    - review_document: fast_general (GPT-5) for review and suggestions
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="tec-docs",
            agent_name="Tec Documents",
            default_risk_tier="green",
            memory_enabled=True,
        )
        self._rule_pack = TecDocumentsSkillPack()

    async def plan_document(
        self,
        document_type: str,
        requirements: dict[str, Any],
        ctx: AgentContext,
    ) -> AgentResult:
        """Plan document structure and sections using LLM.

        GREEN tier — planning only, no file creation.
        """
        if not document_type:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="document.plan",
                status="failed",
                inputs={"document_type": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_DOCUMENT_TYPE"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing document_type")

        return await self.execute_with_llm(
            prompt=(
                f"You are Tec, the document specialist. Plan a document structure.\n\n"
                f"Document type: {document_type}\n"
                f"Requirements: {requirements}\n\n"
                f"Generate a document plan with:\n"
                f"1. Title and subtitle\n"
                f"2. Section outline (heading, description, estimated word count)\n"
                f"3. Required data fields (placeholders)\n"
                f"4. Formatting recommendations (PDF template, fonts, margins)\n"
                f"5. Compliance notes (any legal disclaimers needed)"
            ),
            ctx=ctx,
            event_type="document.plan",
            step_type="classify",
            inputs={"action": "document.plan", "document_type": document_type},
        )

    async def draft_content(
        self,
        document_plan: dict[str, Any],
        business_data: dict[str, Any],
        ctx: AgentContext,
    ) -> AgentResult:
        """Generate document content from a plan using LLM.

        Uses primary_reasoner for high-quality content generation.
        GREEN tier — draft only, requires user review before finalization.
        """
        title = document_plan.get("title", "")
        if not title:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="document.draft",
                status="failed",
                inputs={"title": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_TITLE"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing document title")

        return await self.execute_with_llm(
            prompt=(
                f"You are Tec, the document specialist. Draft the document content.\n\n"
                f"Title: {title}\n"
                f"Plan: {document_plan}\n"
                f"Business data: {business_data}\n\n"
                f"Generate professional content for each section.\n"
                f"Use formal business tone. Replace placeholders with actual data.\n"
                f"Mark any fields that need user input as [USER_INPUT_REQUIRED].\n"
                f"This is a DRAFT — the user will review before finalization."
            ),
            ctx=ctx,
            event_type="document.draft",
            step_type="draft",
            inputs={
                "action": "document.draft",
                "title": title,
                "section_count": len(document_plan.get("sections", [])),
            },
        )

    async def review_document(
        self,
        document_content: str,
        document_type: str,
        ctx: AgentContext,
    ) -> AgentResult:
        """Review a draft document for completeness, tone, and compliance.

        GREEN tier — analysis only, no modifications.
        """
        if not document_content:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="document.review",
                status="failed",
                inputs={"document_type": document_type, "content_length": 0},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["EMPTY_CONTENT"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty document content")

        return await self.execute_with_llm(
            prompt=(
                f"You are Tec, reviewing a {document_type} document.\n\n"
                f"Content (first 3000 chars):\n{document_content[:3000]}\n\n"
                f"Review for:\n"
                f"1. Completeness — any missing sections or [USER_INPUT_REQUIRED] fields\n"
                f"2. Professional tone — appropriate for business communication\n"
                f"3. Accuracy — dates, numbers, names look correct\n"
                f"4. Compliance — legal disclaimers present if needed\n"
                f"5. Formatting — structure and readability\n\n"
                f"Return a structured review report with pass/fail per category."
            ),
            ctx=ctx,
            event_type="document.review",
            step_type="verify",
            inputs={
                "action": "document.review",
                "document_type": document_type,
                "content_length": len(document_content),
            },
        )
