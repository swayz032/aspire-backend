#!/usr/bin/env python3
"""Check if conversational intelligence migrations are already applied."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Load .env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import asyncio
from aspire_orchestrator.services.supabase_client import supabase_select, supabase_rpc


async def check_table_exists(table_name: str) -> bool:
    """Check if a table exists."""
    try:
        result = await supabase_select(
            "information_schema.tables",
            filters={"table_schema": "public", "table_name": table_name},
        )
        return len(result) > 0
    except:
        return False


async def main():
    """Check migration status."""
    print("=" * 60)
    print("CHECKING MIGRATION STATUS")
    print("=" * 60)
    print()

    tables_to_check = [
        ("066", "general_knowledge_chunks"),
        ("066", "general_knowledge_sources"),
        ("067", "communication_knowledge_chunks"),
        ("067", "communication_knowledge_sources"),
        ("068", "agent_episodes"),
        ("068", "agent_semantic_memory"),
    ]

    all_exist = True
    for migration, table in tables_to_check:
        exists = await check_table_exists(table)
        status = "✅ EXISTS" if exists else "❌ MISSING"
        print(f"{status}  Migration {migration}: {table}")
        all_exist = all_exist and exists

    print()
    print("=" * 60)
    if all_exist:
        print("✅ ALL MIGRATIONS ALREADY APPLIED")
        print("   Skipping migration step")
    else:
        print("⚠️  MIGRATIONS NEED TO BE APPLIED")
        print()
        print("   Option 1 (Recommended): Use Supabase Dashboard SQL Editor")
        print("   → Go to: https://supabase.com/dashboard/project/qtuehjqlcmfcascqjjhc/sql/new")
        print("   → Copy/paste each migration file:")
        print("     - backend/infrastructure/supabase/migrations/066_general_knowledge_base.sql")
        print("     - backend/infrastructure/supabase/migrations/067_communication_knowledge_base.sql")
        print("     - backend/infrastructure/supabase/migrations/068_agent_memory.sql")
        print()
        print("   Option 2: Install Supabase CLI and run:")
        print("   → supabase db push")
    print("=" * 60)

    return 0 if all_exist else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
