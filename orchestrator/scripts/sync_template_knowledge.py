"""Sync Template Knowledge — Generate RAG chunks from template_registry.json.

Usage:
    python -m scripts.sync_template_knowledge [--dry-run] [--verbose]
    python scripts/sync_template_knowledge.py [--dry-run] [--verbose]

Reads template_registry.json and generates 3 chunks per template:
  1. Template Specification — what the template is, its lane, risk tier, and requirements
  2. Template Heuristic — when to use this template, selection rules, ICP scenarios
  3. Template Checklist — required fields, validation rules, pre-generation checks

Ingests as domain=template_intelligence using the template_spec chunking strategy.

Law #2: All ingestion produces receipts via the pipeline.
Law #3: Fails closed if registry is missing or malformed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

# Allow running from project root or scripts/ dir
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aspire_orchestrator.services.legal_ingestion_pipeline import (
    IngestResult,
    ingest_file,
)

logger = logging.getLogger("sync_template_knowledge")

# Path to template registry
_REGISTRY_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "template_registry.json"
)

# Domain for template knowledge
_TEMPLATE_DOMAIN = "template_intelligence"

# Lane descriptions for context
_LANE_DESCRIPTIONS: dict[str, str] = {
    "trades": "Construction, plumbing, HVAC, electrical, general contracting, and trade services",
    "accounting": "CPA firms, bookkeeping services, tax preparation, and financial consulting",
    "landlord": "Property management, residential leasing, and landlord operations",
    "general": "Cross-industry templates applicable to any small business",
}

# Risk tier explanations
_RISK_EXPLANATIONS: dict[str, str] = {
    "green": "Safe automation — can be generated without explicit user approval (still produces receipts)",
    "yellow": "Requires user confirmation before generation — involves external communication or financial implications",
    "red": "Requires explicit authority with strong confirmation UX — binding legal or financial commitment",
}


def _load_registry() -> dict:
    """Load and validate template registry JSON.

    Raises:
        FileNotFoundError: If registry file doesn't exist.
        json.JSONDecodeError: If registry is malformed.
        ValueError: If registry structure is invalid.
    """
    if not _REGISTRY_PATH.exists():
        raise FileNotFoundError(f"Template registry not found: {_REGISTRY_PATH}")

    with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
        registry = json.load(f)

    if "templates" not in registry:
        raise ValueError("Registry missing 'templates' key")

    templates = registry["templates"]
    if not isinstance(templates, dict) or len(templates) == 0:
        raise ValueError("Registry 'templates' is empty or not a dict")

    return registry


def _generate_spec_chunk(key: str, tmpl: dict) -> str:
    """Generate specification chunk for a template."""
    lane = tmpl.get("lane", "unknown")
    lane_desc = _LANE_DESCRIPTIONS.get(lane, lane)
    risk = tmpl.get("risk_tier", "unknown")
    risk_desc = _RISK_EXPLANATIONS.get(risk, risk)
    desc = tmpl.get("description", "No description")
    jurisdiction = "Yes" if tmpl.get("jurisdiction_required") else "No"
    pandadoc_uuid = tmpl.get("pandadoc_template_uuid", "")
    has_pandadoc = "Mapped" if pandadoc_uuid else "Not yet mapped"

    return f"""## Specification

Template: {key}
Description: {desc}
Lane: {lane} ({lane_desc})
Risk Tier: {risk} — {risk_desc}
Jurisdiction Required: {jurisdiction}
PandaDoc Template: {has_pandadoc}
Attorney Approved: {"Yes" if tmpl.get("attorney_approved") else "No"}

This template is part of the {lane} lane and is designed for {lane_desc.lower()}.
{'Clara MUST verify the applicable state jurisdiction before generating this template.' if tmpl.get('jurisdiction_required') else 'This template does not require jurisdiction-specific customization.'}
{'PandaDoc template UUID: ' + pandadoc_uuid if pandadoc_uuid else 'PandaDoc template mapping is pending — Clara should use template discovery (pandadoc.templates.list) to find a matching template.'}
"""


def _generate_heuristic_chunk(key: str, tmpl: dict) -> str:
    """Generate heuristic/usage rules chunk for a template."""
    lane = tmpl.get("lane", "unknown")
    desc = tmpl.get("description", "")
    risk = tmpl.get("risk_tier", "unknown")

    # Build scenario text based on template key
    scenarios = _get_scenarios(key, lane)

    return f"""## Heuristic

Template: {key}
Lane: {lane}

### When to Use This Template
{scenarios}

### Selection Rules
- Clara should select this template when the user's intent matches: {desc.lower()}
- Risk tier {risk.upper()} applies — {'user confirmation required before generation' if risk in ('yellow', 'red') else 'can be auto-generated with receipt'}
{'- Jurisdiction state MUST be identified before generation' if tmpl.get('jurisdiction_required') else '- No jurisdiction verification needed'}
- If the user's request is ambiguous between this template and another, Clara should ask for clarification rather than guess (Law #3 — fail closed)

### Related Templates
{_get_related_templates(key, lane)}
"""


def _generate_checklist_chunk(key: str, tmpl: dict) -> str:
    """Generate validation checklist chunk for a template."""
    required = tmpl.get("required_fields", [])
    delta = tmpl.get("required_fields_delta", [])
    all_fields = required + delta
    risk = tmpl.get("risk_tier", "unknown")

    field_list = "\n".join(f"- [ ] {field}" for field in all_fields) if all_fields else "- [ ] No specific fields defined"

    return f"""## Checklist

Template: {key}
Risk Tier: {risk}

### Pre-Generation Validation Checklist
{field_list}
{'- [ ] Jurisdiction state identified and validated' if tmpl.get('jurisdiction_required') else ''}
- [ ] All party names confirmed (sender and client)
- [ ] Client email address collected (required for PandaDoc signing)

### Field Validation Rules
{_get_field_validation(all_fields)}

### Post-Generation Checks
- [ ] Document created in PandaDoc (receipt with document UUID)
- [ ] All merge fields populated (no blank placeholders)
- [ ] Risk tier correctly applied ({risk.upper()})
- [ ] Contract state set to DRAFT in Aspire contracts table
{'- [ ] User approval obtained before sending (YELLOW/RED tier)' if risk in ('yellow', 'red') else ''}

### Error Handling
- Missing required field: Return to user with specific field request (do not generate partial document)
- PandaDoc API failure: Emit failure receipt, retry once with backoff, escalate to user if retry fails
- Template not found in PandaDoc: Fall back to template discovery (pandadoc.templates.list), emit receipt
"""


def _get_scenarios(key: str, lane: str) -> str:
    """Return ICP scenarios for template selection."""
    scenarios: dict[str, str] = {
        "trades_msa_lite": "- Plumber signing a recurring service agreement with a property management company\n- HVAC contractor establishing terms for ongoing maintenance with a commercial building\n- Electrician formalizing relationship with a general contractor for regular subcontracting",
        "trades_sow": "- Contractor defining specific deliverables for a bathroom remodel\n- HVAC company specifying milestones for a new installation project\n- Landscaper detailing seasonal service plan with pricing",
        "trades_estimate_quote_acceptance": "- Plumber providing a binding estimate for a kitchen renovation\n- Electrician sending a quote for panel upgrade with customer acceptance\n- Contractor formalizing a bid that the client wants to accept",
        "trades_work_order": "- Property manager authorizing a plumber to perform a specific repair\n- General contractor issuing work authorization to a subcontractor\n- Facility manager approving an HVAC service call",
        "trades_change_order": "- Homeowner requesting additional work during a remodel (scope change)\n- Contractor discovering unforeseen conditions requiring price adjustment\n- Client adding rooms to a painting project mid-contract",
        "trades_completion_acceptance": "- Homeowner signing off on a completed bathroom renovation\n- Property manager accepting repair work as satisfactorily completed\n- General contractor accepting subcontractor's finished scope",
        "trades_subcontractor_agreement": "- General contractor hiring a plumbing sub for a new construction project\n- Remodeling company bringing on an electrical subcontractor\n- Commercial builder engaging a concrete subcontractor",
        "trades_independent_contractor_agreement": "- Accounting firm hiring a seasonal tax preparer as a 1099 contractor\n- Small business hiring a freelance graphic designer for marketing materials\n- Property manager engaging an independent handyman for on-call repairs",
        "acct_engagement_letter": "- CPA firm onboarding a new bookkeeping client\n- Accounting practice formalizing a tax preparation engagement\n- Bookkeeping service starting monthly reconciliation for a small business",
        "acct_scope_addendum": "- Client requesting payroll services in addition to existing bookkeeping engagement\n- Adding advisory services to an existing tax preparation engagement\n- Expanding scope to include financial statement preparation",
        "acct_access_authorization": "- Client authorizing CPA firm to access QuickBooks Online\n- Bookkeeper getting permission to connect to client's bank via Plaid\n- Accountant receiving access to client's payroll system",
        "acct_fee_schedule_billing_auth": "- Establishing hourly rates and billing frequency for a new engagement\n- Client authorizing automatic ACH payments for monthly bookkeeping fees\n- Setting up retainer billing with auto-replenishment",
        "acct_confidentiality_data_handling_addendum": "- Addressing data handling requirements for a client in a regulated industry\n- Formalizing data retention and destruction policies for client records\n- Adding CCPA/CPRA compliance language to an existing engagement",
        "landlord_residential_lease_base": "- Landlord signing a 12-month lease with a new tenant\n- Property manager generating a standard lease for a rental unit\n- Owner of a duplex creating a lease for the second unit",
        "landlord_lease_addenda_pack": "- Adding a pet policy addendum to an existing lease\n- Creating a smoking policy addendum for a non-smoking building\n- Adding parking assignment and storage unit addenda",
        "landlord_renewal_extension_addendum": "- Renewing a tenant's lease for another year with a rent increase\n- Converting a fixed-term lease to month-to-month\n- Extending a lease for 6 months while a tenant finds a new place",
        "landlord_move_in_checklist": "- Documenting unit condition before a new tenant moves in\n- Creating a photo-documented record of appliance conditions\n- Recording existing damage to protect against security deposit disputes",
        "landlord_move_out_checklist": "- Documenting unit condition after tenant vacates\n- Comparing move-out condition to move-in checklist for deposit deductions\n- Creating evidence for security deposit itemization",
        "landlord_security_deposit_itemization": "- Calculating and documenting deductions from a tenant's security deposit\n- Providing required itemized statement after tenant move-out\n- Returning deposit with detailed breakdown of any withheld amounts",
        "landlord_notice_to_enter": "- Landlord notifying tenant of scheduled maintenance visit\n- Property manager providing notice for annual inspection\n- Notifying tenant of pest control treatment scheduled for unit",
        "general_mutual_nda": "- Two businesses exploring a potential partnership or joint venture\n- Vendor and client sharing confidential information during evaluation\n- Two professionals discussing a potential merger of practices",
        "general_one_way_nda": "- Business owner sharing proprietary processes with a potential contractor\n- Entrepreneur pitching to an investor and sharing business plan\n- Company sharing trade secrets with a vendor for a specific project bid",
    }
    return scenarios.get(key, f"- Standard {lane} scenario requiring {key.replace('_', ' ')}")


def _get_related_templates(key: str, lane: str) -> str:
    """Return related template suggestions."""
    related: dict[str, str] = {
        "trades_msa_lite": "- trades_sow (for specific project scoping under the MSA)\n- general_mutual_nda (if sharing confidential information)",
        "trades_sow": "- trades_msa_lite (establish general terms first, then SOW for specific projects)\n- trades_change_order (for scope modifications after SOW is signed)",
        "trades_subcontractor_agreement": "- trades_sow (for defining specific deliverables under the sub agreement)\n- general_one_way_nda (if sub will access proprietary information)",
        "trades_independent_contractor_agreement": "- general_one_way_nda (if contractor will access confidential systems)\n- acct_access_authorization (if contractor needs system access)",
        "acct_engagement_letter": "- acct_fee_schedule_billing_auth (formalize payment terms)\n- acct_access_authorization (authorize system access)\n- acct_confidentiality_data_handling_addendum (data handling terms)",
        "landlord_residential_lease_base": "- landlord_lease_addenda_pack (pet, smoking, parking policies)\n- landlord_move_in_checklist (document initial condition)",
        "general_mutual_nda": "- trades_msa_lite (after NDA, formalize the working relationship)\n- general_one_way_nda (if only one party is disclosing)",
    }
    return related.get(key, f"- See other {lane} lane templates for related documents")


def _get_field_validation(fields: list[str]) -> str:
    """Generate field validation rules based on field names."""
    rules: list[str] = []
    for field in fields:
        if field == "party_names":
            rules.append("- party_names: Must include at least sender name and client name. Full legal names preferred.")
        elif field == "template_id":
            rules.append("- template_id: Internal Aspire reference. Auto-generated, not user-facing.")
        elif "amount" in field or "rent" in field or "deposit" in field or "price" in field or "fee" in field:
            rules.append(f"- {field}: Must be a positive numeric value. Currency format (e.g., '$1,500.00').")
        elif "date" in field:
            rules.append(f"- {field}: Must be a valid date. ISO 8601 format for API, locale-formatted for display.")
        elif "email" in field:
            rules.append(f"- {field}: Must be a valid email address.")
        elif "address" in field:
            rules.append(f"- {field}: Full street address including city, state, and ZIP code.")
        elif "description" in field or "scope" in field:
            rules.append(f"- {field}: Free text. Should be specific and detailed (minimum 20 characters).")
        elif "term" in field or "period" in field:
            rules.append(f"- {field}: Duration specification (e.g., '12 months', '1 year', '90 days').")
        elif "jurisdiction" in field or "state" in field:
            rules.append(f"- {field}: Must be a valid US state abbreviation (e.g., 'CA', 'TX', 'NY').")
        else:
            rules.append(f"- {field}: Required. Validate non-empty before submission.")
    return "\n".join(rules) if rules else "- No specific field validation rules defined"


async def _sync_templates(
    registry: dict,
    dry_run: bool = False,
) -> list[IngestResult]:
    """Generate and ingest template knowledge chunks.

    For each template, generates a single markdown file with 3 sections
    (spec, heuristic, checklist), then ingests it using the template_spec
    chunking strategy.
    """
    templates = registry["templates"]
    results: list[IngestResult] = []
    total = len(templates)

    for idx, (key, tmpl) in enumerate(sorted(templates.items()), 1):
        logger.info(
            "[%d/%d] Generating knowledge for template: %s (lane=%s, risk=%s)%s",
            idx, total, key, tmpl.get("lane", "?"), tmpl.get("risk_tier", "?"),
            " [DRY RUN]" if dry_run else "",
        )

        # Generate the 3-section markdown
        spec = _generate_spec_chunk(key, tmpl)
        heuristic = _generate_heuristic_chunk(key, tmpl)
        checklist = _generate_checklist_chunk(key, tmpl)

        content = f"<!-- domain: template_intelligence, subdomain: {key}, chunk_strategy: template_spec -->\n\n"
        content += f"# Template: {key}\n\n"
        content += spec + "\n" + heuristic + "\n" + checklist

        if dry_run:
            results.append(IngestResult(
                source_path=f"template:{key}",
                domain=_TEMPLATE_DOMAIN,
                chunks_created=3,  # Expected 3 chunks per template
            ))
            continue

        # Write to a temp file and ingest
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            prefix=f"template_{key}_",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            result = await ingest_file(
                tmp_path,
                domain=_TEMPLATE_DOMAIN,
                source_type="template_registry",
            )
            result.source_path = f"template:{key}"  # Override temp path with logical name
            results.append(result)
            logger.info(
                "  -> created=%d, skipped=%d",
                result.chunks_created,
                result.chunks_skipped,
            )
        except Exception as e:
            logger.error("  -> FAILED: %s", e)
            results.append(IngestResult(
                source_path=f"template:{key}",
                domain=_TEMPLATE_DOMAIN,
                errors=[str(e)],
            ))
        finally:
            # Clean up temp file
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass

    return results


def _print_summary(results: list[IngestResult], dry_run: bool = False) -> None:
    """Print template sync summary."""
    total_created = sum(r.chunks_created for r in results)
    total_skipped = sum(r.chunks_skipped for r in results)
    total_errors = sum(len(r.errors) for r in results)

    mode = "DRY RUN" if dry_run else "SYNC"
    print(f"\n{'=' * 60}")
    print(f"TEMPLATE KNOWLEDGE {mode} SUMMARY")
    print(f"{'=' * 60}")
    print(f"Templates processed: {len(results)}")
    print(f"Chunks created:      {total_created}")
    print(f"Chunks skipped:      {total_skipped} (dedup)")
    print(f"Errors:              {total_errors}")
    print(f"Expected per template: 3 (spec + heuristic + checklist)")
    print(f"{'=' * 60}\n")

    if total_errors > 0:
        print("TEMPLATES WITH ERRORS:")
        for r in results:
            if r.errors:
                for err in r.errors:
                    print(f"  {r.source_path}: {err}")
        print()


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Sync template registry into Clara RAG knowledge base",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be generated without calling the pipeline",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Override path to template_registry.json",
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Override registry path if provided
    if args.registry:
        global _REGISTRY_PATH
        _REGISTRY_PATH = Path(args.registry)

    # Load registry
    try:
        registry = _load_registry()
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logger.error("Failed to load template registry: %s", e)
        return 1

    template_count = len(registry["templates"])
    logger.info("Loaded template registry: %d templates", template_count)

    # Sync
    results = asyncio.run(_sync_templates(registry, dry_run=args.dry_run))

    # Summary
    _print_summary(results, dry_run=args.dry_run)

    has_errors = any(r.errors for r in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
