#!/usr/bin/env python3
"""Apply Supabase migrations 066, 067, 068 via psycopg2."""

import sys
import psycopg2
from pathlib import Path

# Database URL from Railway
DATABASE_URL = "postgresql://postgres.qtuehjqlcmfcascqjjhc:Mbaquan1974%21@aws-1-us-east-1.pooler.supabase.com:6543/postgres"

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "infrastructure" / "supabase" / "migrations"
MIGRATIONS = [
    "066_general_knowledge_base.sql",
    "067_communication_knowledge_base.sql",
    "068_agent_memory.sql",
]

def main():
    print("=" * 70)
    print("APPLYING CONVERSATIONAL INTELLIGENCE MIGRATIONS")
    print("=" * 70)

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False  # Use transactions
    cursor = conn.cursor()

    for migration_file in MIGRATIONS:
        migration_path = MIGRATIONS_DIR / migration_file
        print(f"\n📄 Applying {migration_file}...")

        sql = migration_path.read_text()

        try:
            # Execute the entire file
            cursor.execute(sql)
            conn.commit()
            print(f"✅ {migration_file} applied successfully")
        except psycopg2.errors.DuplicateTable as e:
            conn.rollback()
            print(f"⚠️  {migration_file} - tables already exist (idempotent)")
        except psycopg2.errors.DuplicateObject as e:
            conn.rollback()
            print(f"⚠️  {migration_file} - objects already exist (idempotent)")
        except Exception as e:
            conn.rollback()
            print(f"❌ {migration_file} failed: {e}")
            cursor.close()
            conn.close()
            return 1

    cursor.close()
    conn.close()

    print("\n" + "=" * 70)
    print("✅ ALL MIGRATIONS APPLIED SUCCESSFULLY")
    print("=" * 70)
    print("\nNext steps:")
    print("  1. Run: python scripts/seed_general_knowledge.py")
    print("  2. Run: python scripts/seed_communication_knowledge.py")
    print("  3. Run: python scripts/verify_conversational_intelligence.py")
    return 0

if __name__ == "__main__":
    sys.exit(main())
