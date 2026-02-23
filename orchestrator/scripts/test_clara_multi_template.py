#!/usr/bin/env python3
"""Clara Multi-Template E2E Stress Test -- All 14 Real PandaDoc Templates.

Tests Clara's intelligence pipeline (Layer 1 mechanical + Layer 2 LLM)
across ALL 14 real PandaDoc templates in the registry.

Run from WSL:
  cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator
  source ~/venvs/aspire/bin/activate
  python scripts/test_clara_multi_template.py

Requires:
  - ASPIRE_PANDADOC_API_KEY in .env
  - OPENAI_API_KEY in .env (for LLM Layer 2 token fill)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from aspire_orchestrator.providers.pandadoc_client import (
    execute_pandadoc_contract_generate,
    execute_pandadoc_templates_details,
)
from aspire_orchestrator.services.narration import compose_narration

SIGNER_EMAIL = "tonioswayz32@gmail.com"

# Fake sender profile (Skytech Tower -- the user's company)
SENDER = {
    "company": "Skytech Tower LLC",
    "first_name": "Antonio",
    "last_name": "Towers",
    "email": "antonio@skytechdev.com",
    "address": "1400 Innovation Dr",
    "city": "Austin",
    "state": "TX",
    "zip": "78701",
    "phone": "(512) 555-0199",
    "website": "https://skytechdev.com",
}

OFFICE_ID = "multi-template-test-office"
RUN_TS = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

# Use unique suite_id per scenario to avoid per-suite rate limiting (5/min/suite)
def _suite_id(index: int) -> str:
    return f"multi-template-test-{index:02d}"


@dataclass
class TestScenario:
    """A test scenario for Clara to handle."""
    name: str
    template_id: str
    template_key: str  # for registry lookup
    user_prompt: str  # what the user said to Ava
    client: dict  # client/counterparty info
    terms: dict  # additional terms
    expected_tokens: int  # how many tokens we expect
    risk_tier: str = "yellow"


# == Test Scenarios (all 14 real PandaDoc templates) ==========================

SCENARIOS = [
    # -- TRADES (8) -----------------------------------------------------------
    TestScenario(
        name="HVAC Proposal (16 tokens -- hardest)",
        template_id="A4PQkBwRPKjTT38xGLHicN",
        template_key="trades_hvac_proposal",
        user_prompt="Draft an HVAC proposal for Mike Chen at GreenBuild Properties. They need a full HVAC replacement for their 3-story office building at 500 Oak Lane, Denver CO 80202.",
        client={
            "company": "GreenBuild Properties",
            "first_name": "Mike",
            "last_name": "Chen",
            "email": "mchen@greenbuild.com",
            "address": "500 Oak Lane",
            "city": "Denver",
            "state": "CO",
            "zip": "80202",
            "phone": "(720) 555-0888",
        },
        terms={"scope": "Full HVAC replacement, 3-story office", "budget": "$45,000", "jurisdiction_state": "CO"},
        expected_tokens=16,
    ),
    TestScenario(
        name="Construction Proposal (12 tokens -- full client address)",
        template_id="RMqD3gn7qZRZRPVcMbdnUQ",
        template_key="trades_construction_proposal",
        user_prompt="I need a construction proposal for Sarah Williams at Lakeview HOA. They want a community pool renovation at 1200 Lake Rd, Orlando FL 32801. Budget is around $150K.",
        client={
            "company": "Lakeview HOA",
            "first_name": "Sarah",
            "last_name": "Williams",
            "email": "sarah@lakeviewhoa.org",
            "address": "1200 Lake Rd",
            "city": "Orlando",
            "state": "FL",
            "zip": "32801",
            "phone": "(407) 555-1200",
        },
        terms={"scope": "Community pool renovation", "budget": "$150,000", "jurisdiction_state": "FL"},
        expected_tokens=12,
    ),
    TestScenario(
        name="Painting Proposal (8 tokens -- mixed Sender/Client)",
        template_id="6SUHv5KfZ58umgoLu9vsNm",
        template_key="trades_painting_proposal",
        user_prompt="Create a painting proposal for the Robinson residence. David Robinson, 88 Maple St, Nashville TN 37201. Full exterior repaint, Victorian-era home.",
        client={
            "company": "",
            "first_name": "David",
            "last_name": "Robinson",
            "email": "drobinson@gmail.com",
            "address": "88 Maple St",
            "city": "Nashville",
            "state": "TN",
            "zip": "37201",
        },
        terms={"scope": "Full exterior repaint, Victorian-era home", "budget": "$12,500", "jurisdiction_state": "TN"},
        expected_tokens=8,
    ),
    TestScenario(
        name="Roofing Proposal (6 tokens -- baseline)",
        template_id="7V367zKUvGHFtgnoqT2e7V",
        template_key="trades_roofing_proposal",
        user_prompt="Create a roofing proposal for Martinez Family Trust. Contact person is Carlos Martinez.",
        client={
            "company": "Martinez Family Trust",
            "first_name": "Carlos",
            "last_name": "Martinez",
            "email": "carlos.martinez@gmail.com",
        },
        terms={"scope": "Full roof replacement, 2500 sq ft residential", "budget": "$18,500", "jurisdiction_state": "TX"},
        expected_tokens=6,
    ),
    TestScenario(
        name="Architecture Proposal (project timeline)",
        template_id="xzFYgP5NuaQhfTwsmByuDX",
        template_key="trades_architecture_proposal",
        user_prompt="Draft an architecture proposal for the Westside Community Center redesign. Client is Rachel Adams at Westside Foundation.",
        client={
            "company": "Westside Foundation",
            "first_name": "Rachel",
            "last_name": "Adams",
            "email": "rachel@westsidefoundation.org",
        },
        terms={"scope": "Community center redesign -- modern open floor plan", "budget": "$250,000", "project_timeline": "8 months", "jurisdiction_state": "TX"},
        expected_tokens=6,
    ),
    TestScenario(
        name="Residential Construction (budget + scope)",
        template_id="Yxd5Hd8GxvAkCLvUjN9TwC",
        template_key="trades_residential_construction",
        user_prompt="I need a residential construction proposal for the Garcia family. They want a kitchen and bathroom remodel at their home. Budget is $85,000.",
        client={
            "company": "",
            "first_name": "Maria",
            "last_name": "Garcia",
            "email": "maria.garcia@email.com",
        },
        terms={"scope": "Full kitchen and bathroom remodel", "budget": "$85,000", "jurisdiction_state": "TX"},
        expected_tokens=6,
    ),
    TestScenario(
        name="Residential Contract (RED -- binding)",
        template_id="7kruQeak5EaHZBy92CC4qT",
        template_key="trades_residential_contract",
        user_prompt="Draft a residential construction contract for the Thompson project. John Thompson, 220 Elm Dr, Plano TX 75023. Contract value $175,000, 6-month schedule.",
        client={
            "company": "",
            "first_name": "John",
            "last_name": "Thompson",
            "email": "john.thompson@email.com",
            "address": "220 Elm Dr",
            "city": "Plano",
            "state": "TX",
            "zip": "75023",
            "phone": "(972) 555-3344",
        },
        terms={"scope": "New home construction -- 2400 sq ft", "contract_value": "$175,000", "schedule": "6 months", "jurisdiction_state": "TX"},
        expected_tokens=14,
        risk_tier="red",
    ),
    TestScenario(
        name="Scope of Work (milestones + pricing)",
        template_id="Pc5saWpynSmb4NT63FPZPS",
        template_key="trades_sow",
        user_prompt="Create a scope of work for the website redesign project with TechNova. Three phases: discovery, design, development. Fixed price $45,000.",
        client={
            "company": "TechNova Inc",
            "first_name": "James",
            "last_name": "Park",
            "email": "james@technova.io",
        },
        terms={"scope": "Website redesign -- 3 phases", "milestones": "Discovery, Design, Development", "pricing": "$45,000 fixed", "jurisdiction_state": "TX"},
        expected_tokens=6,
    ),

    # -- ACCOUNTING (2) -------------------------------------------------------
    TestScenario(
        name="Engagement Letter (7 tokens + Document.Value)",
        template_id="rp2knmUFyfhAghLF8E9iB5",
        template_key="acct_engagement_letter",
        user_prompt="Draft an accounting proposal for James Park at TechNova Inc. Monthly bookkeeping + quarterly tax prep, $2,500/month retainer.",
        client={
            "company": "TechNova Inc",
            "first_name": "James",
            "last_name": "Park",
            "email": "james@technova.io",
        },
        terms={"scope": "Monthly bookkeeping + quarterly tax prep", "fee": "$2,500/month", "jurisdiction_state": "TX"},
        expected_tokens=7,
    ),
    TestScenario(
        name="Tax Filing (RED -- 1040/1099)",
        template_id="FLsK6snwy6yPjU4jajrJ5E",
        template_key="acct_tax_filing",
        user_prompt="Prepare a 1040 tax filing form for Robert Chen. Tax year 2025.",
        client={
            "company": "",
            "first_name": "Robert",
            "last_name": "Chen",
            "email": "robert.chen@email.com",
        },
        terms={"tax_year": "2025", "filing_type": "1040", "jurisdiction_state": "TX"},
        expected_tokens=4,
        risk_tier="red",
    ),

    # -- LANDLORD (1) ---------------------------------------------------------
    TestScenario(
        name="Commercial Sublease (Sublessee/Sublessor roles)",
        template_id="VuVk8KwBFLCAJNWhvnofA7",
        template_key="landlord_commercial_sublease",
        user_prompt="I need to sublease part of my office to BlueWave Marketing. Contact is Lisa Tran. We're in Texas.",
        client={
            "company": "BlueWave Marketing",
            "first_name": "Lisa",
            "last_name": "Tran",
            "email": "lisa@bluewave.marketing",
        },
        terms={"jurisdiction_state": "TX"},
        expected_tokens=7,
        risk_tier="red",
    ),

    # -- GENERAL (3) ----------------------------------------------------------
    TestScenario(
        name="Mutual NDA (two-way confidentiality)",
        template_id="aVPGZtb2PCBxvrZokgeRri",
        template_key="general_mutual_nda",
        user_prompt="Draft a mutual NDA with Apex Dynamics. Contact person is Diana Foster. We're exploring a partnership in the drone delivery space.",
        client={
            "company": "Apex Dynamics",
            "first_name": "Diana",
            "last_name": "Foster",
            "email": "diana@apexdynamics.io",
            "address": "900 Tech Park Blvd",
            "city": "San Jose",
            "state": "CA",
            "zip": "95110",
        },
        terms={"purpose": "Drone delivery partnership exploration", "term_length": "2 years", "jurisdiction_state": "CA"},
        expected_tokens=14,
    ),
    TestScenario(
        name="One-Way NDA (disclosing party)",
        template_id="sq8j7CH94xPRu6UbDUm6u8",
        template_key="general_one_way_nda",
        user_prompt="I need a one-way NDA where we disclose proprietary info to Pinnacle Consulting. Contact is Mark Liu.",
        client={
            "company": "Pinnacle Consulting",
            "first_name": "Mark",
            "last_name": "Liu",
            "email": "mliu@pinnacleconsulting.com",
        },
        terms={"purpose": "Proprietary technology evaluation", "disclosing_party": "Skytech Tower LLC", "term_length": "3 years", "jurisdiction_state": "TX"},
        expected_tokens=6,
    ),
    TestScenario(
        name="W-9 Form (taxpayer identification)",
        template_id="dg8UdHiAcncid5KhBTUB7i",
        template_key="general_w9",
        user_prompt="I need a W-9 form filled out for our new vendor, Summit Electric. Contact is Ana Reyes.",
        client={
            "company": "Summit Electric LLC",
            "first_name": "Ana",
            "last_name": "Reyes",
            "email": "ana@summitelectric.com",
        },
        terms={"taxpayer_name": "Summit Electric LLC", "business_type": "LLC"},
        expected_tokens=4,
    ),
]


def _build_payload(scenario: TestScenario) -> dict:
    """Build a PandaDoc generate payload from a scenario, simulating what
    Clara's generate_contract() would produce after LLM param extraction."""
    c = scenario.client
    s = SENDER

    # Build parties in the format pandadoc_client expects
    parties = [
        {
            "name": s["company"],
            "email": s["email"],
            "role": "sender",
            "company": s["company"],
            "contact_name": f"{s['first_name']} {s['last_name']}",
            "first_name": s["first_name"],
            "last_name": s["last_name"],
            "address": s["address"],
            "city": s["city"],
            "state": s["state"],
            "zip": s["zip"],
            "phone": s["phone"],
            "website": s.get("website", ""),
        },
        {
            "name": c.get("company") or f"{c['first_name']} {c['last_name']}",
            "email": c.get("email", SIGNER_EMAIL),
            "role": "client",
            "company": c.get("company", ""),
            "contact_name": f"{c['first_name']} {c['last_name']}",
            "first_name": c["first_name"],
            "last_name": c["last_name"],
            "address": c.get("address", ""),
            "city": c.get("city", ""),
            "state": c.get("state", ""),
            "zip": c.get("zip", ""),
            "phone": c.get("phone", ""),
        },
    ]

    doc_name = f"{scenario.name.split('(')[0].split('--')[0].strip()} - {s['company']} / {c.get('company') or c['last_name']}"

    return {
        "template_id": scenario.template_id,
        "template_type": scenario.template_key,
        "name": doc_name,
        "parties": parties,
        "terms": scenario.terms,
        "context": {
            "company_name": s["company"],
            "owner_name": f"{s['first_name']} {s['last_name']}",
            "owner_email": s["email"],
            "business_name": s["company"],
        },
        "metadata": {
            "aspire_office_id": OFFICE_ID,
            "aspire_template_key": scenario.template_key,
        },
    }


async def run_scenario(scenario: TestScenario, index: int) -> dict:
    """Run a single Clara E2E scenario and return results."""
    suite_id = _suite_id(index)
    correlation_id = f"multi-{RUN_TS}-{index:02d}"
    start = time.monotonic()

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  TEST {index + 1}/{len(SCENARIOS)}: {scenario.name}")
    print(f"  User: \"{scenario.user_prompt}\"")
    print(f"{sep}\n")

    # Step 1: Get template details (what tokens does this template need?)
    print(f"  [1/3] Clara reads template fields...")
    detail_result = await execute_pandadoc_templates_details(
        payload={"template_id": scenario.template_id},
        correlation_id=f"{correlation_id}-detail",
        suite_id=suite_id,
        office_id=OFFICE_ID,
    )

    template_tokens = []
    template_fields = []
    if detail_result.outcome.value == "success":
        template_tokens = detail_result.data.get("tokens", [])
        template_fields = detail_result.data.get("fields", [])
        print(f"        Template has {len(template_tokens)} tokens + {len(template_fields)} fields")
        if template_tokens:
            token_names = [t.get("name", "?") for t in template_tokens]
            print(f"        Tokens: {', '.join(token_names)}")
    else:
        print(f"        WARNING: Could not read template details: {detail_result.error}")

    # Step 2: Clara generates the document
    print(f"\n  [2/3] Clara generates document via PandaDoc API...")
    payload = _build_payload(scenario)
    gen_result = await execute_pandadoc_contract_generate(
        payload=payload,
        correlation_id=f"{correlation_id}-gen",
        suite_id=suite_id,
        office_id=OFFICE_ID,
    )

    elapsed = time.monotonic() - start
    outcome = gen_result.outcome.value
    doc_id = gen_result.data.get("document_id", "")
    fill_rate = gen_result.data.get("token_quality", {}).get("fill_rate_pct", 0)
    missing_tokens = gen_result.data.get("token_quality", {}).get("missing_tokens", [])
    needs_info = gen_result.data.get("needs_info", False)
    needs_additional = gen_result.data.get("needs_additional_info", False)
    suggested_questions = gen_result.data.get("suggested_questions", [])

    # Quality data from Wave 2 intelligence
    quality = gen_result.data.get("document_quality", {})
    confidence = quality.get("confidence_score", 0)
    grade = quality.get("quality_grade", "?")
    specialist_notes = quality.get("specialist_notes", [])
    proactive_warnings = quality.get("proactive_warnings", [])

    # Step 3: Generate Ava's narration
    print(f"\n  [3/3] Ava narrates the result...")
    narration = compose_narration(
        outcome="pending" if outcome == "success" else outcome,
        task_type="contract",
        tool_used="pandadoc.contract.generate",
        execution_params={
            "template_type": scenario.template_key,
            "authority_queue": True,
            "subject_entity": {"display_name": scenario.client.get("company") or f"{scenario.client['first_name']} {scenario.client['last_name']}"},
        },
        execution_result=gen_result.data,
        draft_id=doc_id,
        risk_tier=scenario.risk_tier,
        owner_name="Antonio",
        channel="voice",
    )

    # Results
    status_emoji = "PASS" if outcome == "success" else "NEEDS_INFO" if needs_info else "FAIL"
    print(f"\n  {'- ' * 35}")
    print(f"  RESULT: {status_emoji}")
    print(f"  Outcome: {outcome}")
    if doc_id:
        print(f"  Document ID: {doc_id}")
        print(f"  PandaDoc URL: https://app.pandadoc.com/a/#/documents/{doc_id}")
    print(f"  Fill Rate: {fill_rate}%")
    if confidence:
        print(f"  Quality: Grade {grade} | Confidence {confidence}%")
    if specialist_notes:
        print(f"  Specialist Notes:")
        for note in specialist_notes:
            print(f"    - {note}")
    if proactive_warnings:
        print(f"  Warnings:")
        for w in proactive_warnings:
            print(f"    ! {w}")
    if missing_tokens:
        print(f"  Missing Tokens: {missing_tokens}")
    if needs_additional:
        print(f"  Needs Additional Info: True")
    if suggested_questions:
        print(f"  Clara's Questions:")
        for q in suggested_questions:
            print(f"    - {q}")
    print(f"  Time: {elapsed:.1f}s")
    print(f"\n  AVA SAYS:")
    print(f"    \"{narration}\"")

    return {
        "scenario": scenario.name,
        "template_key": scenario.template_key,
        "outcome": outcome,
        "document_id": doc_id,
        "fill_rate": fill_rate,
        "confidence": confidence,
        "grade": grade,
        "missing_tokens": missing_tokens,
        "specialist_notes": specialist_notes,
        "proactive_warnings": proactive_warnings,
        "needs_info": needs_info,
        "needs_additional": needs_additional,
        "questions": suggested_questions,
        "elapsed_s": round(elapsed, 1),
        "narration": narration,
        "error": gen_result.error,
    }


async def main():
    print("\n" + "=" * 70)
    print("  CLARA 14-TEMPLATE E2E STRESS TEST")
    print(f"  {len(SCENARIOS)} templates | Run: {RUN_TS}")
    print("=" * 70)

    # Check API keys
    api_key = os.environ.get("ASPIRE_PANDADOC_API_KEY", "")
    llm_key = os.environ.get("ASPIRE_OPENAI_API_KEY", "")
    print(f"  PandaDoc API Key: {'SET' if api_key else 'MISSING'}")
    print(f"  OpenAI API Key:   {'SET' if llm_key else 'MISSING (Layer 2 LLM disabled)'}")

    if not api_key:
        print("\n  ERROR: ASPIRE_PANDADOC_API_KEY not set. Cannot run E2E tests.")
        sys.exit(1)

    # Run scenarios with delays to avoid PandaDoc rate limiting
    # PandaDoc sandbox allows ~10 req/min but each scenario makes 2 API calls
    # (template details + document create), so space them 12s apart
    results = []
    for i, scenario in enumerate(SCENARIOS):
        if i > 0:
            print(f"\n  [Waiting 12s for rate limit cooldown...]")
            await asyncio.sleep(12)
        r = await run_scenario(scenario, i)
        results.append(r)

    # == Summary Table ==
    print("\n\n" + "=" * 70)
    print("  FINAL RESULTS SUMMARY")
    print("=" * 70)
    print(f"  {'Template':<40s} {'Result':<12s} {'Fill%':>5s} {'Grade':>5s} {'Conf':>5s} {'Time':>6s}")
    print(f"  {'-'*40} {'-'*12} {'-'*5} {'-'*5} {'-'*5} {'-'*6}")

    passed = 0
    needs_info_count = 0
    failed = 0
    total_confidence = 0
    graded_count = 0
    for r in results:
        name = r["scenario"][:39]
        outcome = r["outcome"]
        fill = f"{r['fill_rate']}%" if r["fill_rate"] else "N/A"
        grade = r["grade"] if r["grade"] != "?" else "-"
        conf = f"{r['confidence']}%" if r["confidence"] else "-"
        t = f"{r['elapsed_s']}s"

        if outcome == "success":
            status = "CREATED"
            passed += 1
            if r["confidence"]:
                total_confidence += r["confidence"]
                graded_count += 1
        elif r["needs_info"]:
            status = "NEEDS_INFO"
            needs_info_count += 1
        else:
            status = "FAILED"
            failed += 1

        print(f"  {name:<40s} {status:<12s} {fill:>5s} {grade:>5s} {conf:>5s} {t:>6s}")

    print(f"\n  TOTALS: {passed} created | {needs_info_count} needs_info | {failed} failed")
    total_docs = sum(1 for r in results if r["document_id"])
    print(f"  Documents created in PandaDoc: {total_docs}")
    if graded_count:
        avg_conf = total_confidence / graded_count
        print(f"  Average Confidence: {avg_conf:.0f}%")

    if any(r["missing_tokens"] for r in results):
        print("\n  DOCUMENTS WITH MISSING TOKENS:")
        for r in results:
            if r["missing_tokens"]:
                print(f"    {r['scenario']}: {r['missing_tokens']}")

    if any(r["proactive_warnings"] for r in results):
        print("\n  PROACTIVE WARNINGS:")
        for r in results:
            for w in r["proactive_warnings"]:
                print(f"    {r['scenario']}: {w}")

    # Save results to JSON for MCP verification
    results_file = Path(__file__).resolve().parent / f"clara_e2e_results_{RUN_TS}.json"
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Results saved to: {results_file}")

    # Print document IDs for MCP verification
    print("\n  DOCUMENT IDs (for PandaDoc verification):")
    for r in results:
        if r["document_id"]:
            print(f"    {r['template_key']}: {r['document_id']}")

    print("\n" + "=" * 70)
    print("  TEST COMPLETE")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
