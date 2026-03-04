#!/usr/bin/env python3
"""Execute migrations 066, 067, 068 via Supabase Admin API."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Load .env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import asyncio
import httpx

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "infrastructure" / "supabase" / "migrations"

MIGRATIONS = [
    "066_general_knowledge_base.sql",
    "067_communication_knowledge_base.sql",
    "068_agent_memory.sql",
]

def execute_sql_via_psycopg2(sql: str) -> tuple[bool, str]:
    """Execute SQL via direct Postgres connection using service role key as password."""
    try:
        import psycopg2
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary"], stdout=subprocess.DEVNULL)
        import psycopg2

    conn_string = os.environ.get("ASPIRE_SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not conn_string:
        return False, "Missing database URL. Set ASPIRE_SUPABASE_DB_URL (preferred) or DATABASE_URL."

    try:
        conn = psycopg2.connect(conn_string)
        conn.autocommit = True
        cursor = conn.cursor()

        print("   Connected to Supabase database")
        print("   Executing SQL file...")

        try:
            cursor.execute(sql)
            cursor.close()
            conn.close()
            print("   Migration executed successfully")
            return True, "OK"
        except Exception as e:
            error_msg = str(e)
            cursor.close()
            conn.close()
            if "already exists" in error_msg.lower() or "duplicate" in error_msg.lower():
                print(f"   Objects already exist (idempotent)")
                return True, "Already exists (idempotent)"
            print(f"\n   Error: {error_msg[:500]}")
            return False, error_msg
    except Exception as e:
        return False, str(e)


def main():
    """Execute all migrations."""
    print("=" * 70)
    print("EXECUTING CONVERSATIONAL INTELLIGENCE MIGRATIONS (066, 067, 068)")
    print("=" * 70)
    print()

    all_success = True
    for migration_file in MIGRATIONS:
        migration_path = MIGRATIONS_DIR / migration_file

        if not migration_path.exists():
            print(f"❌ Migration file not found: {migration_path}")
            return 1

        print(f"\n📄 Applying {migration_file}...")
        sql = migration_path.read_text()

        success, message = execute_sql_via_psycopg2(sql)

        if not success:
            print(f"❌ {migration_file} FAILED: {message}")
            all_success = False
            break
        else:
            print(f"✅ {migration_file} applied: {message}")

    if not all_success:
        return 1

    success = all_success
    message = "All migrations applied successfully"

    if success:
        print("✅ MIGRATIONS APPLIED SUCCESSFULLY")
        print(f"   {message}")
        print()
        print("=" * 70)
        print("NEXT STEPS:")
        print("  1. Run seed scripts:")
        print("     python scripts/seed_general_knowledge.py")
        print("     python scripts/seed_communication_knowledge.py")
        print("  2. Run verification:")
        print("     python scripts/verify_conversational_intelligence.py")
        print("=" * 70)
        return 0
    else:
        print(f"❌ MIGRATION FAILED: {message}")
        print()
        print("Fallback: apply each migration manually via your SQL editor.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
