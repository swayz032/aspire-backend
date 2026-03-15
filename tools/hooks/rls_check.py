#!/usr/bin/env python3
"""GovGuard: RLS Check Hook

Scans SQL migration files for CREATE TABLE statements and verifies they include
Row-Level Security (RLS) policies.

Law #6: Tenant Isolation — zero cross-tenant reads/writes, enforced at DB layer via RLS.

Usage: python tools/hooks/rls_check.py [files...]
Exit code 1 if any migration creates a table without enabling RLS.
"""
import re
import sys
from pathlib import Path


# Tables that legitimately don't need RLS (system/config tables)
EXEMPT_TABLES = {
    'schema_migrations',
    'schema_lock',
    'pg_stat_statements',
    'spatial_ref_sys',
}


def check_migration_file(filepath: str) -> list[str]:
    """Check a SQL migration file for tables missing RLS."""
    violations: list[str] = []
    try:
        content = Path(filepath).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return violations

    # Find all CREATE TABLE statements
    create_pattern = re.compile(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)',
        re.IGNORECASE,
    )

    tables_created = set()
    for match in create_pattern.finditer(content):
        table_name = match.group(1).lower()
        if table_name not in EXEMPT_TABLES:
            tables_created.add(table_name)

    if not tables_created:
        return violations

    # Check for ALTER TABLE ... ENABLE ROW LEVEL SECURITY
    rls_pattern = re.compile(
        r'ALTER\s+TABLE\s+(?:public\.)?(\w+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY',
        re.IGNORECASE,
    )

    tables_with_rls = set()
    for match in rls_pattern.finditer(content):
        tables_with_rls.add(match.group(1).lower())

    # Check for CREATE POLICY
    policy_pattern = re.compile(
        r'CREATE\s+POLICY\s+\w+\s+ON\s+(?:public\.)?(\w+)',
        re.IGNORECASE,
    )

    tables_with_policy = set()
    for match in policy_pattern.finditer(content):
        tables_with_policy.add(match.group(1).lower())

    for table in tables_created:
        if table not in tables_with_rls:
            violations.append(
                f'{filepath}: Table "{table}" created without ENABLE ROW LEVEL SECURITY (Law #6)'
            )
        elif table not in tables_with_policy:
            violations.append(
                f'{filepath}: Table "{table}" has RLS enabled but no CREATE POLICY defined (Law #6)'
            )

    return violations


def main() -> int:
    files = sys.argv[1:]
    if not files:
        return 0

    all_violations: list[str] = []

    for filepath in files:
        if filepath.endswith('.sql'):
            all_violations.extend(check_migration_file(filepath))

    if all_violations:
        print('GovGuard: RLS Check FAILED (Law #6: Tenant Isolation)')
        print('=' * 60)
        for v in all_violations:
            print(f'  {v}')
        print()
        print('Fix: Add ALTER TABLE <name> ENABLE ROW LEVEL SECURITY + CREATE POLICY')
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
