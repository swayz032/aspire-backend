#!/usr/bin/env python3
"""Discover all PandaDoc templates and their field complexity."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from aspire_orchestrator.providers.pandadoc_client import (
    execute_pandadoc_templates_list,
    execute_pandadoc_templates_details,
)

TEMPLATES_TO_DETAIL = [
    ("VuVk8KwBFLCAJNWhvnofA7", "Commercial Sublease Agreement"),
    ("6SUHv5KfZ58umgoLu9vsNm", "Painting Proposal"),
    ("FLsK6snwy6yPjU4jajrJ5E", "1040/1040EZ/1099-MISC"),
    ("rp2knmUFyfhAghLF8E9iB5", "Accounting Proposal"),
    ("Yxd5Hd8GxvAkCLvUjN9TwC", "Residential Construction Proposal"),
    ("A4PQkBwRPKjTT38xGLHicN", "HVAC Proposal"),
    ("7V367zKUvGHFtgnoqT2e7V", "Roofing Proposal"),
    ("xzFYgP5NuaQhfTwsmByuDX", "Architecture Firm Proposal"),
    ("RMqD3gn7qZRZRPVcMbdnUQ", "Construction Proposal"),
    ("dg8UdHiAcncid5KhBTUB7i", "W9 Form"),
    ("7kruQeak5EaHZBy92CC4qT", "Residential Construction Contract"),
    ("Pc5saWpynSmb4NT63FPZPS", "Contractor Scope of Work"),
    ("sq8j7CH94xPRu6UbDUm6u8", "Non Disclosure Agreement (new)"),
    ("aVPGZtb2PCBxvrZokgeRri", "NDA Template (original)"),
]

SUITE_ID = "discover-suite"
OFFICE_ID = "discover-office"


async def get_details(tid: str, tname: str) -> dict | None:
    result = await execute_pandadoc_templates_details(
        payload={"template_id": tid},
        correlation_id="discover-" + tid[:8],
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    if result.outcome.value == "success":
        data = result.data
        tokens = data.get("tokens", [])
        fields = data.get("fields", [])
        roles = data.get("roles", [])
        content_placeholders = data.get("content_placeholders", [])

        sep = "=" * 65
        print(f"\n{sep}")
        print(f"  {tname}")
        print(f"  ID: {tid}")
        print(f"  Tokens: {len(tokens)} | Fields: {len(fields)} | Roles: {len(roles)} | Placeholders: {len(content_placeholders)}")
        print(f"  Complexity Score: {len(tokens) + len(fields) + len(content_placeholders)}")
        print(f"{sep}")

        if tokens:
            print("  TOKENS (merge fields Clara must fill):")
            for t in tokens:
                val = t.get("value", "")
                name = t.get("name", "?")
                print(f"    [{name}] = {val!r}")

        if fields:
            print("  FIELDS (interactive fields assigned to roles):")
            for f in fields:
                fname = f.get("name", "?")
                ftype = f.get("type", "?")
                assigned = f.get("assigned_to", {})
                role = assigned.get("role", "?") if isinstance(assigned, dict) else "?"
                print(f"    [{fname}] type={ftype} role={role}")

        if content_placeholders:
            print("  CONTENT PLACEHOLDERS (rich text blocks):")
            for cp in content_placeholders:
                print(f"    [{cp.get('uuid', '?')[:12]}...] {cp.get('block_id', '?')}")

        if roles:
            print("  ROLES (signing parties):")
            for r in roles:
                rname = r.get("name", "?")
                order = r.get("signing_order", "")
                print(f"    {rname} (order={order})")

        return {
            "id": tid,
            "name": tname,
            "tokens": tokens,
            "fields": fields,
            "roles": roles,
            "content_placeholders": content_placeholders,
            "complexity": len(tokens) + len(fields) + len(content_placeholders),
        }
    else:
        print(f"\n  {tname}: FAILED - {result.error}")
        return None


async def main():
    print("Discovering PandaDoc template details...\n")

    results = []
    for tid, tname in TEMPLATES_TO_DETAIL:
        r = await get_details(tid, tname)
        if r:
            results.append(r)

    # Summary table
    print("\n\n" + "=" * 65)
    print("  COMPLEXITY RANKING")
    print("=" * 65)
    results.sort(key=lambda x: x["complexity"], reverse=True)
    for i, r in enumerate(results, 1):
        print(f"  {i:2d}. {r['name']:<45s} Score: {r['complexity']:3d}  (T:{len(r['tokens'])} F:{len(r['fields'])} CP:{len(r['content_placeholders'])})")


asyncio.run(main())
