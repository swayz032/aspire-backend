#!/usr/bin/env python3
"""Apply Supabase migrations by parsing and executing in correct order."""

import sys
import re
import psycopg2
from pathlib import Path

DATABASE_URL = "postgresql://postgres.qtuehjqlcmfcascqjjhc:Mbaquan1974%21@aws-1-us-east-1.pooler.supabase.com:6543/postgres"

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "infrastructure" / "supabase" / "migrations"
MIGRATIONS = [
    "066_general_knowledge_base.sql",
    "067_communication_knowledge_base.sql",
    "068_agent_memory.sql",
]

def extract_statements(sql):
    """Extract SQL statements in order: CREATE TABLE, CREATE INDEX, CREATE FUNCTION, ALTER, CREATE POLICY."""
    # Remove comments
    sql = re.sub(r'--.*$', '', sql, flags=re.MULTILINE)

    statements = {
        'tables': [],
        'indexes': [],
        'functions': [],
        'policies': [],
        'comments': []
    }

    # Extract CREATE TABLE statements (multi-line, ending with );)
    tables = re.findall(r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+.*?\);', sql, re.DOTALL | re.IGNORECASE)
    statements['tables'] = tables

    # Extract CREATE INDEX statements
    indexes = re.findall(r'CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+.*?;', sql, re.DOTALL | re.IGNORECASE)
    statements['indexes'] = indexes

    # Extract CREATE FUNCTION/PROCEDURE statements
    functions = re.findall(r'CREATE\s+OR\s+REPLACE\s+FUNCTION\s+.*?\$\$;', sql, re.DOTALL | re.IGNORECASE)
    statements['functions'] = functions

    # Extract ALTER TABLE statements
    alters = re.findall(r'ALTER\s+TABLE\s+.*?;', sql, re.DOTALL | re.IGNORECASE)
    statements['policies'].extend(alters)

    # Extract CREATE POLICY statements
    policies = re.findall(r'CREATE\s+POLICY\s+.*?;', sql, re.DOTALL | re.IGNORECASE)
    statements['policies'].extend(policies)

    # Extract COMMENT statements
    comments = re.findall(r'COMMENT\s+ON\s+.*?;', sql, re.DOTALL | re.IGNORECASE)
    statements['comments'] = comments

    return statements

def main():
    print("=" * 70)
    print("APPLYING CONVERSATIONAL INTELLIGENCE MIGRATIONS")
    print("=" * 70)

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cursor = conn.cursor()

    for migration_file in MIGRATIONS:
        migration_path = MIGRATIONS_DIR / migration_file
        print(f"\n📄 Processing {migration_file}...")

        sql = migration_path.read_text()
        stmts = extract_statements(sql)

        # Execute in order
        for category in ['tables', 'indexes', 'policies', 'functions', 'comments']:
            for stmt in stmts[category]:
                try:
                    cursor.execute(stmt)
                except psycopg2.Error as e:
                    if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                        pass  # Idempotent
                    else:
                        print(f"❌ Error: {e}")
                        print(f"Statement: {stmt[:200]}...")
                        cursor.close()
                        conn.close()
                        return 1

        print(f"✅ {migration_file} applied")

    cursor.close()
    conn.close()

    print("\n" + "=" * 70)
    print("✅ ALL MIGRATIONS APPLIED SUCCESSFULLY")
    print("=" * 70)
    print("\nNext steps:")
    print("  python scripts/seed_general_knowledge.py")
    print("  python scripts/seed_communication_knowledge.py")
    return 0

if __name__ == "__main__":
    sys.exit(main())
