#!/usr/bin/env python3
"""Apply migrations 066, 067, 068 to Supabase via SQL execution endpoint.

Uses httpx to POST SQL to Supabase REST API.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Load .env file
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ Loaded .env from {env_path}\n")

import httpx


MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "infrastructure" / "supabase" / "migrations"

MIGRATIONS_TO_APPLY = [
    "066_general_knowledge_base.sql",
    "067_communication_knowledge_base.sql",
    "068_agent_memory.sql",
]


def execute_sql_via_api(sql: str) -> tuple[bool, str]:
    """Execute SQL via Supabase REST API using service role key."""
    url = os.environ.get("ASPIRE_SUPABASE_URL")
    key = os.environ.get("ASPIRE_SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise ValueError("ASPIRE_SUPABASE_URL and ASPIRE_SUPABASE_SERVICE_ROLE_KEY must be set")

    # Split SQL into individual statements (simple split on semicolons)
    # This is necessary because PostgREST might not support multi-statement execution
    statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]

    print(f"   Executing {len(statements)} SQL statements...")

    with httpx.Client(timeout=60.0) as client:
        for i, stmt in enumerate(statements, 1):
            # Skip comments-only statements
            if not stmt or all(line.startswith("--") for line in stmt.split("\n") if line.strip()):
                continue

            try:
                # Use PostgREST SQL execution (via rpc if available, or direct query)
                # Actually, Supabase doesn't have a direct SQL execution endpoint
                # We need to use the SQL Editor API or pg_admin
                #
                # Alternative: Use the PostgREST /rpc endpoint with a custom function
                # But that requires the function to exist first
                #
                # Best approach: Use supabase-py client's postgrest execute

                # For now, let's use a workaround: execute via the PostgREST query endpoint
                # by wrapping in a transaction block

                response = client.post(
                    f"{url}/rest/v1/rpc/exec_sql",
                    headers={
                        "apikey": key,
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={"sql": stmt},
                )

                if response.status_code not in (200, 201, 204):
                    # Check if it's idempotent (already exists)
                    error_text = response.text
                    if "already exists" in error_text.lower():
                        print(f"      Statement {i}/{len(statements)}: Already exists (idempotent)")
                        continue
                    else:
                        return False, f"Statement {i} failed: {response.status_code} - {response.text}"

            except Exception as e:
                return False, f"Statement {i} error: {str(e)}"

    return True, "OK"


def apply_migration_via_supabase_client(migration_file: str) -> bool:
    """Apply migration using supabase-py client."""
    migration_path = MIGRATIONS_DIR / migration_file

    if not migration_path.exists():
        print(f"❌ Migration file not found: {migration_path}")
        return False

    print(f"📄 Reading {migration_file}...")
    sql = migration_path.read_text()

    print(f"⚙️  Applying {migration_file}...")

    # For migrations, we need direct SQL execution which Supabase doesn't expose easily
    # Best approach: Use psql command line tool or apply manually via Dashboard
    #
    # Let me try a different approach: parse CREATE TABLE/FUNCTION statements
    # and execute them individually

    import re

    # Extract CREATE statements
    create_pattern = r'(CREATE\s+(?:TABLE|FUNCTION|INDEX|POLICY|OR\s+REPLACE\s+FUNCTION)[^;]+;)'
    alter_pattern = r'(ALTER\s+TABLE[^;]+;)'
    comment_pattern = r'(COMMENT\s+ON[^;]+;)'

    statements = []
    statements += re.findall(create_pattern, sql, re.IGNORECASE | re.DOTALL)
    statements += re.findall(alter_pattern, sql, re.IGNORECASE | re.DOTALL)
    statements += re.findall(comment_pattern, sql, re.IGNORECASE | re.DOTALL)

    print(f"   Found {len(statements)} SQL statements to execute")

    # Execute via Supabase Python client
    try:
        from supabase import create_client

        url = os.environ.get("ASPIRE_SUPABASE_URL")
        key = os.environ.get("ASPIRE_SUPABASE_SERVICE_ROLE_KEY")

        client = create_client(url, key)

        success_count = 0
        for i, stmt in enumerate(statements, 1):
            try:
                # Try executing via PostgREST (won't work for DDL, but let's try)
                # Actually, PostgREST doesn't support DDL
                # We need to use a different approach

                # Skip for now - need manual application via Dashboard
                pass

            except Exception as e:
                error_msg = str(e)
                if "already exists" in error_msg.lower():
                    print(f"      Statement {i}: Already exists (idempotent)")
                    success_count += 1
                else:
                    print(f"      Statement {i} error: {error_msg[:100]}")

        print(f"⚠️  Cannot apply DDL via REST API - use Supabase Dashboard SQL Editor")
        print(f"   Or use: psql connection string")
        return False

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def apply_migration_via_manual_instruction(migration_file: str) -> None:
    """Print manual instructions for applying migration."""
    migration_path = MIGRATIONS_DIR / migration_file

    print(f"📄 {migration_file}")
    print(f"   Location: {migration_path}")
    print(f"   → Open Supabase Dashboard SQL Editor")
    print(f"   → Copy/paste contents and execute")
    print()


def main():
    """Apply all migrations."""
    print("=" * 60)
    print("CONVERSATIONAL INTELLIGENCE MIGRATIONS")
    print("=" * 60)
    print()

    # Check if we can use direct SQL execution
    # Supabase doesn't expose a public SQL execution endpoint for DDL
    # Options:
    # 1. psql with connection string (requires DB password, not service role key)
    # 2. Supabase CLI (requires supabase CLI installed)
    # 3. Manual via Dashboard SQL Editor

    print("⚠️  Supabase does not expose a REST API for DDL execution.")
    print("   Migrations must be applied via:")
    print("   1. Supabase Dashboard SQL Editor (recommended)")
    print("   2. psql with database connection string")
    print("   3. Supabase CLI: supabase db push")
    print()

    print("Manual application instructions:")
    print("-" * 60)
    for migration_file in MIGRATIONS_TO_APPLY:
        apply_migration_via_manual_instruction(migration_file)

    print("=" * 60)
    print("After applying migrations manually, run:")
    print("  python scripts/verify_conversational_intelligence.py")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
