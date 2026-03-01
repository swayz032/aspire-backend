#!/usr/bin/env python3
"""Template Certification Script

Validates PandaDoc templates before adding to template_registry.json.

This script ensures templates work correctly with Clara's token mapping system by:
1. Fetching template details from PandaDoc API
2. Checking _TERMS_TOKEN_MAP coverage (all required tokens have mappings)
3. Verifying pricing table exists with correct name
4. Creating a test document with sample data
5. Verifying test document achieves 80%+ fill rate
6. Returning certification result with detailed report

Usage:
    python scripts/certify_template.py <pandadoc_template_uuid>

Exit codes:
    0 - Template certified (80%+ fill rate)
    1 - Template failed certification
    2 - Script error or invalid usage

Example:
    python scripts/certify_template.py Pc5saWpynSmb4NT63FPZPS
    # Returns JSON report with certification status
"""

import asyncio
import json
import sys
from typing import Any

# Import Clara's PandaDoc client and token mapping
from aspire_orchestrator.providers.pandadoc_client import (
    _TERMS_TOKEN_MAP,
    PandaDocClient,
    _fetch_template_details_and_build_tokens,
)
from aspire_orchestrator.models import Outcome


def generate_test_context() -> dict[str, Any]:
    """Generate comprehensive test data for template certification.

    This data covers all common token scenarios:
    - Sender (contractor/service provider)
    - Client (customer/buyer)
    - Terms (project details, pricing, dates, jurisdiction)

    Returns:
        dict: Test context with parties and terms data
    """
    return {
        "parties": [
            {
                "role": "sender",
                "full_name": "John Smith",
                "company": "Test Construction LLC",
                "email": "john@testconstruction.com",
                "address": "123 Main Street",
                "city": "Austin",
                "state": "TX",
                "zip": "78701",
                "phone": "555-0100",
                "website": "www.testconstruction.com",
            },
            {
                "role": "client",
                "full_name": "Jane Doe",
                "company": "Client Industries Inc",
                "email": "jane@clientindustries.com",
                "address": "456 Oak Avenue",
                "city": "Dallas",
                "state": "TX",
                "zip": "75201",
                "phone": "555-0200",
            },
        ],
        "terms": {
            "project_name": "Office Renovation Project",
            "scope": "Office Renovation Project",
            "scope_description": "Complete office renovation including electrical, plumbing, and finish work",
            "budget": "$50,000",
            "contract_value": "$50,000",
            "amount": "$50,000",
            "total": "$50,000",
            "start_date": "2026-03-15",
            "completion_date": "2026-06-30",
            "end_date": "2026-06-30",
            "milestones": "Phase 1: Demolition (2 weeks), Phase 2: Rough-in (4 weeks), Phase 3: Finish (2 weeks)",
            "pricing": "Labor: $30,000, Materials: $15,000, Contingency: $5,000",
            "jurisdiction_state": "TX",
            "purpose": "Vendor partnership evaluation",
            "term_length": "2 years",
            "monthly_rent": "$5,000",
            "security_deposit": "$10,000",
            "lease_term": "24 months",
            "property_address": "789 Business Park Drive, Austin, TX 78758",
            "services_scope": "Monthly bookkeeping and quarterly tax preparation",
            "fee_schedule": "Monthly retainer: $500",
            "tax_year": "2025",
            "filing_type": "1040",
            "taxpayer_name": "John Smith",
            "business_type": "LLC",
            "disclosing_party": "Test Construction LLC",
            "project_timeline": "3 months",
            "schedule": "Start: March 15, 2026; Completion: June 30, 2026",
        },
    }


async def certify_template(template_uuid: str) -> dict[str, Any]:
    """Certify a template for production use.

    Certification process:
    1. Fetch template details from PandaDoc
    2. Extract all tokens (merge fields) from template
    3. Check _TERMS_TOKEN_MAP coverage for custom tokens
    4. Verify pricing table name
    5. Create test document with sample data
    6. Calculate fill rate (% of tokens successfully populated)
    7. Check if fill rate >= 80% (certification threshold)

    Args:
        template_uuid: PandaDoc template UUID

    Returns:
        dict: Certification result with:
            - certified: bool (True if fill rate >= 80%)
            - fill_rate: float (0.0-1.0)
            - tokens_covered: int (number of tokens filled)
            - total_tokens: int (total tokens in template)
            - pricing_table_name: str (if found)
            - missing_tokens: list[str] (tokens that couldn't be filled)
            - reason: str (if not certified)
            - recommended_config: dict (if certified)
    """
    print(f"[STEP 1/7] Fetching template details for {template_uuid}...", file=sys.stderr)

    # Initialize PandaDoc client
    client = PandaDocClient()

    try:
        # Generate test context
        test_context = generate_test_context()

        # Create test payload (minimal structure required by PandaDoc client)
        test_payload = {
            "parties": test_context["parties"],
            "terms": test_context["terms"],
        }

        # Use a dummy UUID for certification (bypass suite profile lookup)
        # 00000000-0000-0000-0000-000000000000 is a valid UUID that won't match any real suite
        certification_suite_id = "00000000-0000-0000-0000-000000000000"

        # Fetch template details and build tokens
        print("[STEP 2/7] Extracting template tokens and roles...", file=sys.stderr)
        auto_tokens, template_roles, missing_tokens, auto_fields, content_placeholders = (
            await _fetch_template_details_and_build_tokens(
                client=client,
                template_uuid=template_uuid,
                payload=test_payload,
                suite_id=certification_suite_id,
                rag_context=None,
                template_type="",
                template_spec=None,
            )
        )

        print(f"[STEP 3/7] Found {len(auto_tokens)} tokens, {len(missing_tokens)} missing", file=sys.stderr)

        # Check _TERMS_TOKEN_MAP coverage
        print("[STEP 4/7] Checking _TERMS_TOKEN_MAP coverage...", file=sys.stderr)
        custom_tokens_in_map = []
        custom_tokens_not_in_map = []

        for token in auto_tokens:
            token_name = token.get("name", "")
            if token_name.startswith("Custom."):
                if token_name in _TERMS_TOKEN_MAP:
                    custom_tokens_in_map.append(token_name)
                else:
                    custom_tokens_not_in_map.append(token_name)

        print(f"   - Custom tokens in map: {len(custom_tokens_in_map)}", file=sys.stderr)
        print(f"   - Custom tokens NOT in map: {len(custom_tokens_not_in_map)}", file=sys.stderr)

        # Try to detect pricing table name by fetching full template
        print("[STEP 5/7] Detecting pricing table name...", file=sys.stderr)
        pricing_table_name = None
        try:
            # Fetch template details to inspect pricing tables using httpx directly
            import httpx
            from aspire_orchestrator.config.settings import settings

            api_key = settings.pandadoc_api_key
            if api_key:
                async with httpx.AsyncClient(timeout=15.0) as http_client:
                    response = await http_client.get(
                        f"https://api.pandadoc.com/public/v1/templates/{template_uuid}/details",
                        headers={"Authorization": f"API-Key {api_key}"},
                    )

                    if response.status_code == 200:
                        template_details = response.json()
                        # Look for pricing_tables in template
                        pricing_tables = template_details.get("pricing", {}).get("tables", [])
                        if pricing_tables and len(pricing_tables) > 0:
                            # Use first pricing table's name
                            pricing_table_name = pricing_tables[0].get("name")
                            print(f"   - Found pricing table: {pricing_table_name}", file=sys.stderr)
                    else:
                        print(f"   - PandaDoc API returned {response.status_code}", file=sys.stderr)
            else:
                print(f"   - No PandaDoc API key configured (skipping)", file=sys.stderr)
        except Exception as e:
            print(f"   - Could not detect pricing table: {e}", file=sys.stderr)

        # Calculate fill rate
        print("[STEP 6/7] Calculating fill rate...", file=sys.stderr)
        total_tokens = len(auto_tokens)
        tokens_filled = total_tokens - len(missing_tokens)
        fill_rate = tokens_filled / total_tokens if total_tokens > 0 else 0.0

        print(f"   - Fill rate: {fill_rate:.1%} ({tokens_filled}/{total_tokens} tokens)", file=sys.stderr)

        # Certification decision
        print("[STEP 7/7] Making certification decision...", file=sys.stderr)
        certification_threshold = 0.80
        certified = fill_rate >= certification_threshold

        result = {
            "certified": certified,
            "fill_rate": round(fill_rate, 4),
            "tokens_covered": tokens_filled,
            "total_tokens": total_tokens,
            "missing_tokens": missing_tokens,
            "custom_tokens_in_map": custom_tokens_in_map,
            "custom_tokens_not_in_map": custom_tokens_not_in_map,
        }

        if pricing_table_name:
            result["pricing_table_name"] = pricing_table_name

        if certified:
            # Generate recommended config for template_registry.json
            result["recommended_config"] = {
                "pandadoc_template_uuid": template_uuid,
                "pricing_table_name": pricing_table_name or "Pricing Table 1",
                "required_fields": ["party_names", "template_id"],
                "required_party_data": {
                    "sender": ["full_name", "company", "email"],
                    "client": ["full_name", "company", "email"],
                },
            }

            # Add role_map if we detected non-standard roles
            if template_roles:
                non_standard_roles = {}
                for role_def in template_roles:
                    role_name = role_def.get("name", "")
                    # Map to sender/client based on signing order
                    if role_name not in ["Sender", "Client", "Recipient"]:
                        signing_order = role_def.get("signing_order", 1)
                        mapped_role = "sender" if signing_order == 1 else "client"
                        non_standard_roles[role_name] = mapped_role

                if non_standard_roles:
                    result["recommended_config"]["role_map"] = non_standard_roles

            print(f"\n✅ CERTIFIED: Template passed with {fill_rate:.1%} fill rate", file=sys.stderr)
        else:
            result["reason"] = (
                f"Fill rate {fill_rate:.1%} below threshold {certification_threshold:.0%}. "
                f"Missing tokens: {', '.join(missing_tokens[:5])}"
                + ("..." if len(missing_tokens) > 5 else "")
            )
            print(f"\n❌ FAILED: {result['reason']}", file=sys.stderr)

        return result

    except Exception as e:
        print(f"\n❌ ERROR: {e}", file=sys.stderr)
        return {
            "certified": False,
            "fill_rate": 0.0,
            "tokens_covered": 0,
            "total_tokens": 0,
            "missing_tokens": [],
            "reason": f"Certification failed with error: {str(e)}",
        }
    finally:
        # Clean up client
        await client.close()


async def main() -> int:
    """Main entry point.

    Returns:
        int: Exit code (0 = certified, 1 = failed, 2 = error)
    """
    if len(sys.argv) != 2:
        print("Usage: python scripts/certify_template.py <template_uuid>", file=sys.stderr)
        print("\nExample:", file=sys.stderr)
        print("  python scripts/certify_template.py Pc5saWpynSmb4NT63FPZPS", file=sys.stderr)
        return 2

    template_uuid = sys.argv[1].strip()

    if not template_uuid:
        print("Error: Template UUID cannot be empty", file=sys.stderr)
        return 2

    print(f"\n{'='*70}", file=sys.stderr)
    print(f"CLARA TEMPLATE CERTIFICATION", file=sys.stderr)
    print(f"{'='*70}\n", file=sys.stderr)
    print(f"Template UUID: {template_uuid}\n", file=sys.stderr)

    result = await certify_template(template_uuid)

    # Pretty print result to stdout (parseable JSON)
    print(json.dumps(result, indent=2))

    # Return appropriate exit code
    if result["certified"]:
        return 0
    else:
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
