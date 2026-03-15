"""Clara Legal Skill Pack -- Contract management, e-signatures via PandaDoc.

Clara is the Legal desk. She handles:
  - Contract generation from templates (YELLOW -- requires user confirmation)
  - Contract review / status read (GREEN -- read-only)
  - Contract signing via e-signature (RED -- binding, irreversible, requires presence)
  - Compliance tracking for expirations/renewals (GREEN -- read-only)

Provider: PandaDoc (https://api.pandadoc.com/public/v1)

Law compliance:
  - Law #1: Skill pack proposes, orchestrator decides
  - Law #2: Every method emits a receipt (success, failure, and denial)
  - Law #3: Fail closed on missing parameters, missing binding fields
  - Law #4: generate=YELLOW, review/compliance=GREEN, sign=RED (presence + authority)
  - Law #5: Capability tokens required for all PandaDoc tool calls
  - Law #6: suite_id/office_id scoping enforced in every operation
  - Law #7: Uses tool_executor for all PandaDoc calls (tools are hands)

Binding fields enforcement (per policy_matrix.yaml):
  - contract.generate: party_names, template_id
  - contract.sign: contract_id, signer_name, signer_email
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.openai_client import generate_text_async, parse_json_text
from aspire_orchestrator.services.tool_executor import execute_tool
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)

ACTOR_CLARA = "skillpack:clara-legal"
RECEIPT_VERSION = "1.0"


def _mask_email(email: str) -> str:
    """Mask email for receipts: j***@acme.com (Law #9: PII redaction)."""
    if not email or "@" not in email:
        return "<EMAIL_REDACTED>"
    local, domain = email.rsplit("@", 1)
    return f"{local[0]}***@{domain}" if local else f"***@{domain}"


def _mask_name(name: str) -> str:
    """Mask person name for receipts: J. S*** (Law #9: PII redaction)."""
    if not name:
        return "<NAME_REDACTED>"
    parts = name.strip().split()
    if len(parts) == 1:
        return f"{parts[0][0]}***"
    return f"{parts[0][0]}. {parts[-1][0]}***"


def _redact_parties(parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact PII from party list for receipt metadata."""
    redacted = []
    for p in parties:
        rp = dict(p)
        if "email" in rp:
            rp["email"] = _mask_email(rp["email"])
        if "name" in rp:
            rp["name"] = _mask_name(rp["name"])
        redacted.append(rp)
    return redacted

# ---------------------------------------------------------------------------
# Template Registry — dynamic loading from template_registry.json
# ---------------------------------------------------------------------------

_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "template_registry.json"

_TEMPLATE_REGISTRY: dict[str, Any] = {}
_LEGACY_ALIASES: dict[str, str | None] = {}


def _load_template_registry() -> None:
    """Load template registry from JSON. Called once at module import."""
    global _TEMPLATE_REGISTRY, _LEGACY_ALIASES
    try:
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _TEMPLATE_REGISTRY = data.get("templates", {})
        _LEGACY_ALIASES = data.get("legacy_aliases", {})
        logger.info("Loaded %d templates from registry", len(_TEMPLATE_REGISTRY))
    except Exception as e:
        logger.error("Failed to load template registry from %s: %s", _REGISTRY_PATH, e)
        _TEMPLATE_REGISTRY = {}
        _LEGACY_ALIASES = {}


_load_template_registry()


def _resolve_template_key(raw_key: str) -> str:
    """Resolve legacy aliases (nda -> general_mutual_nda) and return canonical key.

    Case-insensitive: LLM often extracts "NDA" or "Mutual NDA" — must match
    lowercase registry keys and aliases.
    """
    # Exact match first (fast path)
    if raw_key in _TEMPLATE_REGISTRY:
        return raw_key

    # Normalize: lowercase, strip, replace spaces with underscores
    normalized = raw_key.lower().strip().replace(" ", "_")

    # Try normalized against registry
    if normalized in _TEMPLATE_REGISTRY:
        return normalized

    # Try original and normalized against aliases
    alias = _LEGACY_ALIASES.get(raw_key) or _LEGACY_ALIASES.get(normalized)
    if alias and alias in _TEMPLATE_REGISTRY:
        return alias

    # Try partial match: "mutual_nda" → "general_mutual_nda"
    for key in _TEMPLATE_REGISTRY:
        if normalized in key or key.endswith(f"_{normalized}"):
            return key

    return raw_key  # Return as-is for validation to catch


def get_template_spec(template_key: str) -> dict[str, Any] | None:
    """Look up a template by key. Returns None if not found."""
    resolved = _resolve_template_key(template_key)
    return _TEMPLATE_REGISTRY.get(resolved)


def get_template_risk_tier(template_key: str) -> str:
    """Get the risk tier for a template (default: yellow)."""
    spec = get_template_spec(template_key)
    if spec:
        return spec.get("risk_tier", "yellow")
    return "yellow"


# Dynamically built from registry + legacy aliases
VALID_TEMPLATE_TYPES = frozenset(
    list(_TEMPLATE_REGISTRY.keys())
    + [k for k, v in _LEGACY_ALIASES.items() if v is not None]
)

# Binding fields per policy_matrix.yaml -- must be confirmed by user
CONTRACT_GENERATE_BINDING_FIELDS = {"party_names", "template_id"}
CONTRACT_SIGN_BINDING_FIELDS = {"contract_id", "signer_name", "signer_email"}


@dataclass
class SkillPackResult:
    """Result of a Clara Legal skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False
    presence_required: bool = False


@dataclass
class ClaraContext:
    """Tenant-scoped execution context for Clara operations."""

    suite_id: str
    office_id: str
    correlation_id: str
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """Compute SHA256 hash of inputs for receipt linkage."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _make_receipt(
    *,
    ctx: ClaraContext,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    tool_used: str = "",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    redactions: list[str] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Clara legal operation (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-clara-{uuid.uuid4().hex[:12]}",
        "ts": now,
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_CLARA,
        "correlation_id": ctx.correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": _compute_inputs_hash(inputs or {}),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "clara-legal-v1",
            "reasons": [] if outcome == "success" else [reason_code],
        },
        "redactions": redactions or [],
    }
    if tool_used:
        receipt["tool_used"] = tool_used
    if metadata:
        receipt["metadata"] = metadata
    return receipt


def _check_binding_fields(
    params: dict[str, Any],
    required_fields: set[str],
) -> list[str]:
    """Return list of missing binding fields (Law #3: fail closed)."""
    missing = []
    for f in sorted(required_fields):
        val = params.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(f)
        elif isinstance(val, list) and len(val) == 0:
            missing.append(f)
    return missing


def preflight_validate(template_key: str, terms: dict[str, Any]) -> list[str]:
    """Validate terms against template's required_fields_delta.

    Returns list of error messages. Empty list = valid.
    Checks required_fields_delta from template spec — fields beyond the
    standard binding fields that are needed for this specific template.

    RAG enhancement: checks template-specific validation rules from knowledge base.
    """
    spec = get_template_spec(template_key)
    if not spec:
        return [f"Unknown template: {template_key}"]

    errors: list[str] = []
    delta_fields = spec.get("required_fields_delta", [])

    for field_name in delta_fields:
        val = terms.get(field_name)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"Missing required field '{field_name}' for template '{template_key}'")

    # Jurisdiction check (consolidated — also enforced in generate_contract)
    if spec.get("jurisdiction_required"):
        jur = (terms.get("jurisdiction_state") or "").strip()
        if not jur:
            errors.append(f"Missing jurisdiction_state for template '{template_key}' (required)")

    # RAG: check template-specific validation rules (non-blocking, informational)
    try:
        import asyncio
        from aspire_orchestrator.services.legal_retrieval_service import get_retrieval_service
        svc = get_retrieval_service()
        # Sync context: try to get running loop, fall through if not available
        try:
            loop = asyncio.get_running_loop()
            # Can't await in sync function — skip RAG in sync context
            # RAG will be applied in the async generate_contract call instead
        except RuntimeError:
            pass  # No event loop — skip RAG
    except Exception:
        pass  # Non-fatal — RAG is additive

    return errors


class ClaraLegalSkillPack:
    async def templates_list(self, query: str | None, context: ClaraContext) -> SkillPackResult:
        return await self.browse_templates(query=query, context=context)

    async def templates_details(self, template_id: str, context: ClaraContext) -> SkillPackResult:
        return await self.get_template_details(template_id=template_id, context=context)

    async def contract_generate(self, template_type: str, parties: list[dict[str, Any]], terms: dict[str, Any], context: ClaraContext) -> SkillPackResult:
        return await self.generate_contract(template_type=template_type, parties=parties, terms=terms, context=context)

    async def contract_review(self, contract_id: str, context: ClaraContext) -> SkillPackResult:
        return await self.review_contract(contract_id=contract_id, context=context)

    async def contract_sign(self, contract_id: str, signer_name: str, signer_email: str, context: ClaraContext) -> SkillPackResult:
        return await self.sign_contract(contract_id=contract_id, signer_name=signer_name, signer_email=signer_email, context=context)

    async def contract_compliance(self, contract_id: str, context: ClaraContext) -> SkillPackResult:
        return await self.track_compliance(contract_id=contract_id, context=context)

    """Clara Legal Skill Pack -- governed contract management operations.

    All methods require a ClaraContext for tenant scoping (Law #6)
    and produce receipts for every outcome (Law #2).

    Risk tiers (Law #4):
      - generate_contract: YELLOW (requires user confirmation)
      - review_contract: GREEN (read-only)
      - sign_contract: RED (requires presence + explicit authority)
      - track_compliance: GREEN (read-only)
    """

    async def browse_templates(
        self,
        query: str | None,
        context: ClaraContext,
    ) -> SkillPackResult:
        """Browse PandaDoc template library (GREEN -- read-only, no approval needed).

        Clara uses this to discover what templates are available in PandaDoc,
        then recommends the right one to the user.

        RAG enhancement: semantic template search before PandaDoc API call.

        Args:
            query: Optional search query (e.g., "NDA", "lease", "contractor")
            context: Tenant-scoped execution context
        """
        # RAG: semantic template search (graceful degradation)
        rag_template_matches: list[dict[str, Any]] = []
        if query:
            try:
                from aspire_orchestrator.services.legal_retrieval_service import get_retrieval_service
                svc = get_retrieval_service()
                rag_results = await svc.retrieve(
                    query=query,
                    suite_id=context.suite_id,
                    method_context="browse_templates",
                )
                if rag_results.chunks:
                    rag_template_matches = [
                        {
                            "template_key": c.get("template_key", ""),
                            "domain": c.get("domain", ""),
                            "relevance": c.get("combined_score", 0),
                            "content_preview": c.get("content", "")[:200],
                        }
                        for c in rag_results.chunks
                        if c.get("template_key")
                    ]
            except Exception as e:
                logger.warning("RAG template search failed (non-fatal): %s", e)

        payload: dict[str, Any] = {}
        if query:
            payload["q"] = query

        result: ToolExecutionResult = await execute_tool(
            tool_id="pandadoc.templates.list",
            payload=payload,
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "success" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _make_receipt(
            ctx=context,
            action_type="templates.list",
            risk_tier="green",
            outcome=status,
            reason_code="EXECUTED" if result.outcome == Outcome.SUCCESS else "TOOL_FAILED",
            tool_used="pandadoc.templates.list",
            inputs={"action": "templates.list", "query": query or ""},
            metadata={
                "template_count": result.data.get("count", 0) if result.data else 0,
            },
        )

        response_data = result.data or {}
        if rag_template_matches:
            response_data["rag_template_matches"] = rag_template_matches

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=response_data,
            receipt=receipt,
            error=result.error,
        )

    async def get_template_details(
        self,
        template_id: str,
        context: ClaraContext,
    ) -> SkillPackResult:
        """Get template field requirements from PandaDoc (GREEN -- read-only).

        Clara uses this to discover what merge fields, tokens, and roles a template
        requires, then tells the user exactly what information is needed.

        Args:
            template_id: PandaDoc template ID
            context: Tenant-scoped execution context
        """
        if not template_id or not template_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="templates.details",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_TEMPLATE_ID",
                tool_used="pandadoc.templates.details",
                inputs={"action": "templates.details", "template_id": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: template_id",
            )

        result: ToolExecutionResult = await execute_tool(
            tool_id="pandadoc.templates.details",
            payload={"template_id": template_id.strip()},
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "success" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _make_receipt(
            ctx=context,
            action_type="templates.details",
            risk_tier="green",
            outcome=status,
            reason_code="EXECUTED" if result.outcome == Outcome.SUCCESS else "TOOL_FAILED",
            tool_used="pandadoc.templates.details",
            inputs={"action": "templates.details", "template_id": template_id.strip()},
            metadata={
                "template_id": template_id.strip(),
                "field_count": result.data.get("field_count", 0) if result.data else 0,
                "token_count": result.data.get("token_count", 0) if result.data else 0,
                "role_count": result.data.get("role_count", 0) if result.data else 0,
            },
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def generate_contract(
        self,
        template_type: str,
        parties: list[dict[str, Any]],
        terms: dict[str, Any],
        context: ClaraContext,
    ) -> SkillPackResult:
        """Generate a contract from a template (YELLOW/RED -- requires user approval).

        Args:
            template_type: One of VALID_TEMPLATE_TYPES or legacy alias (nda, msa, etc.)
            parties: List of party dicts [{name, email, role}]
            terms: Contract terms dict (title, description, duration, etc.)
            context: Tenant-scoped execution context

        Binding fields: party_names, template_id
        """
        # Resolve legacy aliases (nda -> general_mutual_nda)
        resolved_key = _resolve_template_key(template_type) if template_type else ""
        template_spec = get_template_spec(template_type) if template_type else None

        # Validate template_type
        if not template_type or template_spec is None:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.generate",
                risk_tier="yellow",
                outcome="denied",
                reason_code="INVALID_TEMPLATE_TYPE",
                tool_used="pandadoc.contract.generate",
                inputs={"action": "contract.generate", "template_type": template_type},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Invalid template_type: '{template_type}'. "
                f"Must be one of: {', '.join(sorted(VALID_TEMPLATE_TYPES))}",
            )

        # Determine risk tier from registry (RED for landlord_residential_lease_base)
        risk_tier = template_spec.get("risk_tier", "yellow")

        # Jurisdiction check — fail closed if required but missing (Law #3)
        # Use .strip() to reject whitespace-only values (policy-gate P1 fix)
        jurisdiction_state = (terms.get("jurisdiction_state") or "").strip()
        if template_spec.get("jurisdiction_required") and not jurisdiction_state:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.generate",
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="MISSING_JURISDICTION",
                tool_used="pandadoc.contract.generate",
                inputs={
                    "action": "contract.generate",
                    "template_type": resolved_key,
                    "jurisdiction_required": True,
                },
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Template '{resolved_key}' requires jurisdiction_state in terms "
                "(e.g., 'NY', 'CA'). This is required for legal compliance.",
            )

        # Extract party names for binding field check
        party_names = [p.get("name", "") for p in parties] if parties else []

        params = {
            "party_names": party_names,
            "template_id": resolved_key,
        }

        missing = _check_binding_fields(params, CONTRACT_GENERATE_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.generate",
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="pandadoc.contract.generate",
                inputs={"action": "contract.generate", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        # Preflight: check required_fields_delta from template spec
        preflight_errors = preflight_validate(resolved_key, terms)
        if preflight_errors:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.generate",
                risk_tier=risk_tier,
                outcome="denied",
                reason_code="PREFLIGHT_VALIDATION_FAILED",
                tool_used="pandadoc.contract.generate",
                inputs={
                    "action": "contract.generate",
                    "template_type": resolved_key,
                    "preflight_errors": preflight_errors,
                },
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Preflight validation failed: {'; '.join(preflight_errors)}",
            )

        # RAG: inject jurisdiction rules + business context (graceful degradation)
        rag_context = None
        try:
            from aspire_orchestrator.services.legal_retrieval_service import get_retrieval_service
            svc = get_retrieval_service()
            # Build RAG query from template type + jurisdiction
            rag_query_parts = [resolved_key.replace("_", " ")]
            if jurisdiction_state:
                rag_query_parts.append(f"jurisdiction {jurisdiction_state}")
            rag_query_parts.append("contract requirements clauses")
            rag_results = await svc.retrieve(
                query=" ".join(rag_query_parts),
                suite_id=context.suite_id,
                method_context="generate_contract",
            )
            if rag_results.chunks:
                rag_context = svc.assemble_rag_context(rag_results)
        except Exception as e:
            logger.warning("RAG retrieval for generate_contract failed (non-fatal): %s", e)

        # Build the plan with resolved key and risk tier
        presence_required = risk_tier == "red"
        generate_plan: dict[str, Any] = {
            "template_type": resolved_key,
            "template_lane": template_spec.get("lane", "general"),
            "parties": parties,
            "terms": terms,
            "party_names": party_names,
            "risk_tier": risk_tier,
            "binding_fields": sorted(CONTRACT_GENERATE_BINDING_FIELDS),
            "pandadoc_template_uuid": template_spec.get("pandadoc_template_uuid", ""),
        }
        if presence_required:
            generate_plan["presence_required"] = True
        if rag_context:
            generate_plan["rag_context"] = rag_context

        # Token hints: tell the brain what fields this template needs
        # so Ava can proactively ask the user BEFORE execution
        delta_fields = template_spec.get("required_fields_delta", [])
        if delta_fields:
            generate_plan["template_required_fields"] = delta_fields
            # Check which delta fields are already provided in terms
            provided = [f for f in delta_fields if terms.get(f)]
            missing_delta = [f for f in delta_fields if not terms.get(f)]
            if missing_delta:
                generate_plan["fields_still_needed"] = missing_delta
                generate_plan["message_for_brain"] = (
                    f"Clara will need these fields to fill the template properly: "
                    f"{', '.join(missing_delta)}. Consider asking the user for them "
                    f"before approving execution."
                )

        receipt = _make_receipt(
            ctx=context,
            action_type="contract.generate",
            risk_tier=risk_tier,
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="pandadoc.contract.generate",
            inputs={
                "action": "contract.generate",
                "template_type": resolved_key,
                "party_names": party_names,
            },
            metadata={
                "template_type": resolved_key,
                "template_lane": template_spec.get("lane", "general"),
                "party_count": len(parties),
                "party_names": party_names,
                "risk_tier": risk_tier,
            },
        )

        return SkillPackResult(
            success=True,
            data=generate_plan,
            receipt=receipt,
            approval_required=True,
            presence_required=presence_required,
        )

    async def review_contract(
        self,
        contract_id: str,
        context: ClaraContext,
    ) -> SkillPackResult:
        """Review a contract -- read status and details (GREEN -- read-only).

        Args:
            contract_id: PandaDoc document ID
            context: Tenant-scoped execution context
        """
        if not contract_id or not contract_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.review",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_CONTRACT_ID",
                tool_used="pandadoc.contract.read",
                inputs={"action": "contract.review", "contract_id": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: contract_id",
            )

        # GREEN tier: execute directly via tool_executor (no approval needed)
        result: ToolExecutionResult = await execute_tool(
            tool_id="pandadoc.contract.read",
            payload={"document_id": contract_id.strip()},
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "success" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _make_receipt(
            ctx=context,
            action_type="contract.review",
            risk_tier="green",
            outcome=status,
            reason_code="EXECUTED" if result.outcome == Outcome.SUCCESS else "TOOL_FAILED",
            tool_used="pandadoc.contract.read",
            inputs={"action": "contract.review", "contract_id": contract_id.strip()},
            metadata={
                "contract_id": contract_id.strip(),
                "tool_id": result.tool_id,
            },
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def sign_contract(
        self,
        contract_id: str,
        signer_info: dict[str, Any],
        context: ClaraContext,
    ) -> SkillPackResult:
        """Sign a contract via e-signature (RED -- requires presence + explicit authority).

        This is a binding legal action. Per CLAUDE.md Law #4 and Law #8:
          - RED tier requires explicit authority + strong confirmation UX
          - Presence required (video authority for binding signature)
          - Approval binding enforced (approve-then-swap defense)

        Args:
            contract_id: PandaDoc document ID
            signer_info: Dict with signer_name, signer_email
            context: Tenant-scoped execution context

        Binding fields: contract_id, signer_name, signer_email
        """
        signer_name = signer_info.get("signer_name", "")
        signer_email = signer_info.get("signer_email", "")

        params = {
            "contract_id": contract_id,
            "signer_name": signer_name,
            "signer_email": signer_email,
        }

        missing = _check_binding_fields(params, CONTRACT_SIGN_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.sign",
                risk_tier="red",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="pandadoc.contract.sign",
                inputs={"action": "contract.sign", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        # RAG: pre-sign jurisdiction check (graceful degradation)
        jurisdiction_requirements = None
        try:
            from aspire_orchestrator.services.legal_retrieval_service import get_retrieval_service
            svc = get_retrieval_service()
            rag_results = await svc.retrieve(
                query="e-signature validity witness notarization requirements",
                suite_id=context.suite_id,
                method_context="sign_contract",
            )
            if rag_results.chunks:
                jurisdiction_requirements = svc.assemble_rag_context(rag_results)
        except Exception as e:
            logger.warning("RAG jurisdiction check for sign_contract failed (non-fatal): %s", e)

        # RED tier: build the plan, mark approval_required AND presence_required
        sign_plan: dict[str, Any] = {
            "contract_id": contract_id,
            "signer_name": signer_name,
            "signer_email": signer_email,
            "risk_tier": "red",
            "binding_fields": sorted(CONTRACT_SIGN_BINDING_FIELDS),
            "presence_required": True,
        }
        if jurisdiction_requirements:
            sign_plan["jurisdiction_requirements"] = jurisdiction_requirements

        receipt = _make_receipt(
            ctx=context,
            action_type="contract.sign",
            risk_tier="red",
            outcome="success",
            reason_code="APPROVAL_AND_PRESENCE_REQUIRED",
            tool_used="pandadoc.contract.sign",
            inputs={
                "action": "contract.sign",
                "contract_id": contract_id,
                "signer_name": _mask_name(signer_name),
                "signer_email": _mask_email(signer_email),
            },
            metadata={
                "contract_id": contract_id,
                "signer_name": _mask_name(signer_name),
                "signer_email": _mask_email(signer_email),
                "signature_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            redactions=["signer_name", "signer_email"],
        )

        return SkillPackResult(
            success=True,
            data=sign_plan,
            receipt=receipt,
            approval_required=True,
            presence_required=True,
        )

    async def track_compliance(
        self,
        contract_id: str,
        context: ClaraContext,
    ) -> SkillPackResult:
        """Track contract compliance -- expiration and renewal detection (GREEN -- read-only).

        Reads contract status and checks for upcoming expirations,
        renewal deadlines, and compliance milestones.

        Args:
            contract_id: PandaDoc document ID
            context: Tenant-scoped execution context
        """
        if not contract_id or not contract_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.compliance",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_CONTRACT_ID",
                tool_used="pandadoc.contract.read",
                inputs={"action": "contract.compliance", "contract_id": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: contract_id",
            )

        # GREEN tier: read contract status to check compliance
        result: ToolExecutionResult = await execute_tool(
            tool_id="pandadoc.contract.read",
            payload={"document_id": contract_id.strip()},
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        if result.outcome != Outcome.SUCCESS:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.compliance",
                risk_tier="green",
                outcome="failed",
                reason_code="TOOL_FAILED",
                tool_used="pandadoc.contract.read",
                inputs={"action": "contract.compliance", "contract_id": contract_id.strip()},
                metadata={"tool_id": result.tool_id},
            )
            return SkillPackResult(
                success=False,
                data=result.data,
                receipt=receipt,
                error=result.error,
            )

        # Build compliance assessment from contract data (LLM-enhanced when available)
        compliance_data = await _intelligent_compliance_assessment(
            result.data, contract_id.strip(), suite_id=context.suite_id,
        )

        receipt = _make_receipt(
            ctx=context,
            action_type="contract.compliance",
            risk_tier="green",
            outcome="success",
            reason_code="EXECUTED",
            tool_used="pandadoc.contract.read",
            inputs={"action": "contract.compliance", "contract_id": contract_id.strip()},
            metadata={
                "contract_id": contract_id.strip(),
                "compliance_status": compliance_data.get("compliance_status", "unknown"),
                "expiration_date": compliance_data.get("expiration_date"),
            },
        )

        return SkillPackResult(
            success=True,
            data=compliance_data,
            receipt=receipt,
        )


def _assess_compliance(
    contract_data: dict[str, Any],
    contract_id: str,
) -> dict[str, Any]:
    """Assess compliance status from contract data.

    Checks for:
    - Document status (draft, sent, completed, expired, voided)
    - Expiration date proximity
    - Renewal needs
    """
    status = contract_data.get("status", "unknown")
    expiration_date = contract_data.get("expiration_date")
    name = contract_data.get("name", "")

    compliance_status = "active"
    alerts: list[str] = []

    if status in ("voided", "declined"):
        compliance_status = "terminated"
        alerts.append(f"Contract {status}")
    elif status == "document.draft":
        compliance_status = "pending"
        alerts.append("Contract still in draft -- not yet executed")
    elif status in ("document.sent", "document.waiting_approval"):
        compliance_status = "awaiting_signature"
        alerts.append("Contract sent but not yet signed")

    if expiration_date:
        compliance_data_with_expiry: dict[str, Any] = {
            "contract_id": contract_id,
            "name": name,
            "status": status,
            "compliance_status": compliance_status,
            "expiration_date": expiration_date,
            "alerts": alerts,
            "needs_renewal": compliance_status == "active",
        }
        return compliance_data_with_expiry

    return {
        "contract_id": contract_id,
        "name": name,
        "status": status,
        "compliance_status": compliance_status,
        "expiration_date": None,
        "alerts": alerts,
        "needs_renewal": False,
    }


async def _intelligent_compliance_assessment(
    contract_data: dict[str, Any],
    contract_id: str,
    suite_id: str = "",
) -> dict[str, Any]:
    """LLM-enhanced compliance assessment — builds on deterministic Layer 1.

    Layer 1: Existing deterministic _assess_compliance() (fast, free, reliable).
    Layer 2: GPT-5.2 analyzes contract context, jurisdiction requirements,
    expiration risk scoring, and generates proactive recommendations.

    Graceful degradation: LLM unavailable → returns Layer 1 output exactly.
    """
    # Layer 1: deterministic baseline (always runs)
    base = _assess_compliance(contract_data, contract_id)

    # Layer 2: LLM enhancement (optional — enriches, never replaces)
    try:
        from aspire_orchestrator.config.settings import resolve_openai_api_key, settings

        if not resolve_openai_api_key():
            return base

        # Parse expiration for risk scoring
        expiration_date = base.get("expiration_date")
        days_until_expiry: int | None = None
        urgency_level = "none"
        if expiration_date:
            try:
                from datetime import datetime, timezone
                if isinstance(expiration_date, str):
                    exp_dt = datetime.fromisoformat(expiration_date.replace("Z", "+00:00"))
                    delta = exp_dt - datetime.now(timezone.utc)
                    days_until_expiry = delta.days
                    if days_until_expiry < 30:
                        urgency_level = "urgent"
                    elif days_until_expiry < 60:
                        urgency_level = "warning"
                    elif days_until_expiry < 90:
                        urgency_level = "watch"
            except (ValueError, TypeError):
                pass

        # Enrich base with expiration risk scoring (deterministic)
        if days_until_expiry is not None:
            base["days_until_expiry"] = days_until_expiry
            base["urgency_level"] = urgency_level

        # RAG context for jurisdiction-specific compliance
        rag_context = ""
        try:
            from aspire_orchestrator.services.legal_retrieval_service import get_retrieval_service
            svc = get_retrieval_service()
            name = contract_data.get("name", "contract")
            rag_result = await svc.retrieve(
                query=f"compliance requirements renewal obligations {name}",
                suite_id=suite_id, method_context="compliance_assessment",
            )
            if rag_result.chunks:
                rag_context = svc.assemble_rag_context(rag_result)
        except Exception:
            pass

        preferred_model = getattr(settings, "ava_llm_model", None)
        if not isinstance(preferred_model, str) or not preferred_model.strip():
            preferred_model = getattr(settings, "router_model_reasoner", None)
        model = preferred_model if isinstance(preferred_model, str) and preferred_model.strip() else "gpt-5-mini"
        _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))
        system_role = "developer" if _is_reasoning else "system"
        prompt = (
            f"Contract ID: {contract_id}\n"
            f"Contract Name: {contract_data.get('name', '')}\n"
            f"Current Status: {base.get('status', 'unknown')}\n"
            f"Compliance Status: {base.get('compliance_status', 'unknown')}\n"
            f"Expiration Date: {base.get('expiration_date')}\n"
            f"Days Until Expiry: {base.get('days_until_expiry')}\n"
            f"Urgency Level: {base.get('urgency_level', 'none')}\n"
            f"Existing Alerts: {json.dumps(base.get('alerts', []), default=str)}\n"
            f"Needs Renewal: {base.get('needs_renewal', False)}\n\n"
            f"Contract Data:\n{json.dumps(contract_data, default=str)[:2500]}\n"
        )
        if rag_context:
            prompt += f"\nRelevant Legal Compliance Context:\n{rag_context[:2500]}\n"
        prompt += (
            "\nReturn strict JSON with keys: "
            "specialist_assessment (string), "
            "recommended_actions (array of strings), "
            "risk_score (integer 0-100)."
        )

        content = await generate_text_async(
            model=model,
            messages=[
                {
                    "role": system_role,
                    "content": (
                        "You are Clara, a legal contract compliance specialist. "
                        "Assess contract compliance status and provide actionable recommendations. "
                        "Be specific and professional. Return ONLY JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            api_key=resolve_openai_api_key(),
            base_url=settings.openai_base_url,
            timeout_seconds=float(settings.openai_timeout_seconds),
            max_output_tokens=400,
            temperature=None if _is_reasoning else 0.1,
            prefer_responses_api=True,
        )

        llm_data = parse_json_text(content)
        if isinstance(llm_data, dict):
            base["specialist_assessment"] = llm_data.get("specialist_assessment", "")
            base["recommended_actions"] = llm_data.get("recommended_actions", [])
            base["risk_score"] = llm_data.get("risk_score", 50)
            logger.info(
                "Clara intelligent compliance: risk_score=%d for contract %s",
                base.get("risk_score", -1), contract_id[:8],
            )

    except Exception as e:
        logger.warning("Clara intelligent compliance LLM failed (using Layer 1): %s", e)

    return base


# =============================================================================
# Phase 3 W5a: Enhanced Clara Legal with LLM reasoning + dual approval
# =============================================================================

from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.services.dual_approval_service import (
    get_dual_approval_service,
    ApprovalStatus,
)
from aspire_orchestrator.services.idempotency_service import get_idempotency_service


class EnhancedClaraLegal(EnhancedSkillPack):
    """LLM-enhanced Clara Legal — RED-tier contract intelligence.

    Extends ClaraLegalSkillPack with:
    - review_contract_terms: GPT-5.2 analyzes contract terms for risks
    - plan_signature_flow: GPT-5.2 builds e-signature workflow
    - assess_compliance_risk: GPT-5.2 evaluates compliance exposure
    - initiate_dual_approval: Creates dual approval for contract signing

    ALL methods use high_risk_guard (GPT-5.2) — no cheap models for legal.
    Idempotency enforced on all state-changing operations.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="clara-legal",
            agent_name="Clara Legal",
            default_risk_tier="red",
        )
        self._rule_pack = ClaraLegalSkillPack()

    async def review_contract_terms(
        self, contract_text: str, contract_type: str, ctx: AgentContext,
    ) -> AgentResult:
        """Review contract terms for risks and issues. GREEN — analysis only."""
        if not contract_text:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.review_terms",
                status="failed", inputs={"length": 0},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["EMPTY_CONTRACT"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty contract text")

        if contract_type and get_template_spec(contract_type) is None:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.review_terms",
                status="failed", inputs={"type": contract_type},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["INVALID_CONTRACT_TYPE"]}
            await self.emit_receipt(receipt)
            return AgentResult(
                success=False, receipt=receipt,
                error=f"Invalid contract type: {contract_type}",
            )

        # RAG: inject clause standards, red flags, compliance patterns
        rag_section = ""
        try:
            from aspire_orchestrator.services.legal_retrieval_service import get_retrieval_service
            svc = get_retrieval_service()
            rag_query = f"{contract_type or 'contract'} clause standards red flags compliance review"
            rag_results = await svc.retrieve(
                query=rag_query,
                suite_id=ctx.suite_id,
                method_context="review_contract_terms",
            )
            if rag_results.chunks:
                rag_section = (
                    f"\n\nUse this legal knowledge to guide your review:\n"
                    f"{svc.assemble_rag_context(rag_results)}\n"
                )
        except Exception as e:
            logger.warning("RAG retrieval for review_contract_terms failed (non-fatal): %s", e)

        return await self.execute_with_llm(
            prompt=(
                f"You are Clara, the legal specialist. Review this contract.{rag_section}\n\n"
                f"Contract Type: {contract_type or 'unspecified'}\n"
                f"Contract Text (first 3000 chars):\n{contract_text[:3000]}\n\n"
                f"{'Using the legal knowledge above, analyze' if rag_section else 'Analyze'}:\n"
                f"1. Missing clauses compared to legal standards\n"
                f"2. Deviations from standard language\n"
                f"3. Missing jurisdiction-specific requirements\n"
                f"4. Red flags from compliance patterns\n"
                f"5. Risk rating: LOW/MEDIUM/HIGH with justification\n"
                f"Also analyze: key terms, potential risks, unusual clauses, "
                f"missing protections, liability exposure, termination conditions, "
                f"IP ownership, non-compete scope, indemnification coverage."
            ),
            ctx=ctx, event_type="contract.review_terms", step_type="verify",
            inputs={
                "action": "contract.review_terms",
                "type": contract_type or "unspecified",
                "length": len(contract_text),
                "rag_chunks": len(rag_results.chunks) if rag_section else 0,
            },
        )

    async def plan_signature_flow(
        self, contract_details: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Plan the e-signature workflow. RED — requires approval to execute."""
        contract_id = contract_details.get("contract_id", "")
        if not contract_id:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.plan_signature",
                status="failed", inputs={"contract_id": ""},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_CONTRACT_ID"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing contract ID")

        signers = contract_details.get("signers", [])
        if not signers:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.plan_signature",
                status="failed", inputs={"contract_id": contract_id, "signers": 0},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["NO_SIGNERS"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="No signers specified")

        return await self.execute_with_llm(
            prompt=(
                f"You are Clara, planning an e-signature flow. RED-tier — binding legal action.\n\n"
                f"Contract: {contract_id}\n"
                f"Type: {contract_details.get('type', 'unknown')}\n"
                f"Signers: {len(signers)}\n"
                f"Signer Details: {json.dumps(signers, default=str)}\n\n"
                f"Plan: signing order, PandaDoc API calls, presence verification "
                f"for each signer, dual approval requirements, notification strategy, "
                f"expiration policy, rollback if any signer declines."
            ),
            ctx=ctx, event_type="contract.plan_signature", step_type="plan",
            inputs={
                "action": "contract.plan_signature",
                "contract_id": contract_id,
                "signer_count": len(signers),
            },
        )

    async def assess_compliance_risk(
        self, contracts: list, ctx: AgentContext,
    ) -> AgentResult:
        """Assess compliance risk across contract portfolio. GREEN — analysis only."""
        if not contracts:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.compliance_risk",
                status="failed", inputs={"count": 0},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["NO_CONTRACTS"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="No contracts to assess")

        return await self.execute_with_llm(
            prompt=(
                f"You are Clara, assessing compliance risk across a contract portfolio.\n\n"
                f"Portfolio: {len(contracts)} contracts\n"
                f"Summary: {json.dumps(contracts[:10], default=str)}\n\n"
                f"Assess: expiring contracts (30/60/90 day windows), "
                f"unsigned contracts aging, compliance gaps, renewal priorities, "
                f"risk score by category, recommended actions ranked by urgency."
            ),
            ctx=ctx, event_type="contract.compliance_risk", step_type="verify",
            inputs={"action": "contract.compliance_risk", "count": len(contracts)},
        )

    def initiate_dual_approval(
        self, contract_details: dict, ctx: AgentContext,
    ) -> dict:
        """Create dual approval request for contract signing (legal + business)."""
        svc = get_dual_approval_service()
        binding = {
            "contract_id": contract_details.get("contract_id", ""),
            "signer_name": contract_details.get("signer_name", ""),
            "signer_email": contract_details.get("signer_email", ""),
        }

        result = svc.create_request(
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
            correlation_id=ctx.correlation_id,
            action_type="contract.sign",
            binding_fields=binding,
            required_roles=["legal", "business_owner"],
        )

        return {
            "success": result.success,
            "request_id": result.request_id,
            "status": result.status.value,
            "remaining_roles": result.remaining_roles,
            "receipt": result.receipt,
            "error": result.error,
        }
