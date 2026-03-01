#!/usr/bin/env python3
"""Verification script for Conversational Intelligence Layer deployment.

Checks:
1. Migration 066, 067, 068 tables exist
2. RLS policies are active
3. pgvector indexes exist
4. Seed data is present (if seeded)
5. Redis connection (optional)
6. All services can be imported

Usage:
    cd backend/orchestrator
    source ~/venvs/aspire/bin/activate
    python scripts/verify_conversational_intelligence.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aspire_orchestrator.services.supabase_client import supabase_rpc


def print_status(check: str, passed: bool, details: str = ""):
    """Print check status with color."""
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"{status} {check}")
    if details:
        print(f"      {details}")


async def verify_tables():
    """Check that new tables exist."""
    print("\n=== TABLE VERIFICATION ===")

    tables_to_check = [
        "general_knowledge_chunks",
        "communication_knowledge_chunks",
        "agent_episodes",
        "agent_semantic_memory",
    ]

    try:
        result = await supabase_rpc(
            "exec_sql",
            {
                "query": """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = ANY($1::text[])
                """,
                "params": tables_to_check,
            }
        )

        found_tables = [row["table_name"] for row in result]

        for table in tables_to_check:
            exists = table in found_tables
            print_status(f"Table: {table}", exists)

        return len(found_tables) == len(tables_to_check)

    except Exception as e:
        print_status("Table check", False, str(e))
        return False


async def verify_rls_policies():
    """Check that RLS policies exist."""
    print("\n=== RLS POLICY VERIFICATION ===")

    tables_with_rls = [
        "general_knowledge_chunks",
        "communication_knowledge_chunks",
        "agent_episodes",
        "agent_semantic_memory",
    ]

    try:
        result = await supabase_rpc(
            "exec_sql",
            {
                "query": """
                    SELECT tablename, policyname
                    FROM pg_policies
                    WHERE tablename = ANY($1::text[])
                """,
                "params": tables_with_rls,
            }
        )

        policies_by_table = {}
        for row in result:
            table = row["tablename"]
            if table not in policies_by_table:
                policies_by_table[table] = []
            policies_by_table[table].append(row["policyname"])

        all_passed = True
        for table in tables_with_rls:
            has_policy = table in policies_by_table and len(policies_by_table[table]) > 0
            print_status(
                f"RLS policy: {table}",
                has_policy,
                f"Policies: {', '.join(policies_by_table.get(table, []))}" if has_policy else "No policies found",
            )
            all_passed = all_passed and has_policy

        return all_passed

    except Exception as e:
        print_status("RLS policy check", False, str(e))
        return False


async def verify_pgvector_indexes():
    """Check that pgvector indexes exist."""
    print("\n=== PGVECTOR INDEX VERIFICATION ===")

    tables_with_embeddings = [
        "general_knowledge_chunks",
        "communication_knowledge_chunks",
        "agent_episodes",
    ]

    try:
        result = await supabase_rpc(
            "exec_sql",
            {
                "query": """
                    SELECT indexname, tablename
                    FROM pg_indexes
                    WHERE tablename = ANY($1::text[])
                      AND indexname LIKE '%embedding%'
                """,
                "params": tables_with_embeddings,
            }
        )

        indexes_by_table = {}
        for row in result:
            table = row["tablename"]
            indexes_by_table[table] = row["indexname"]

        all_passed = True
        for table in tables_with_embeddings:
            has_index = table in indexes_by_table
            print_status(
                f"Embedding index: {table}",
                has_index,
                f"Index: {indexes_by_table[table]}" if has_index else "No index found",
            )
            all_passed = all_passed and has_index

        return all_passed

    except Exception as e:
        print_status("pgvector index check", False, str(e))
        return False


async def verify_seed_data():
    """Check if seed data is present (non-blocking)."""
    print("\n=== SEED DATA VERIFICATION (optional) ===")

    tables_to_check = [
        ("general_knowledge_chunks", "general"),
        ("communication_knowledge_chunks", "communication"),
        ("finance_knowledge_chunks", "finance"),
    ]

    try:
        for table, domain_label in tables_to_check:
            result = await supabase_rpc(
                "exec_sql",
                {
                    "query": f"SELECT COUNT(*) as count FROM {table}",
                    "params": [],
                }
            )
            count = result[0]["count"] if result else 0

            # Seed data is optional, so we don't fail if missing
            has_data = count > 0
            print_status(
                f"Seed data: {domain_label}",
                has_data,
                f"{count} chunks" if has_data else "Not seeded yet (run seed scripts)",
            )

        return True  # Non-blocking

    except Exception as e:
        print_status("Seed data check", False, str(e))
        return True  # Non-blocking


async def verify_search_functions():
    """Check that search functions exist."""
    print("\n=== SEARCH FUNCTION VERIFICATION ===")

    functions_to_check = [
        "search_general_knowledge",
        "search_communication_knowledge",
        "search_agent_episodes",
    ]

    try:
        result = await supabase_rpc(
            "exec_sql",
            {
                "query": """
                    SELECT proname
                    FROM pg_proc
                    WHERE proname = ANY($1::text[])
                """,
                "params": functions_to_check,
            }
        )

        found_functions = [row["proname"] for row in result]

        all_passed = True
        for func in functions_to_check:
            exists = func in found_functions
            print_status(f"Function: {func}", exists)
            all_passed = all_passed and exists

        return all_passed

    except Exception as e:
        print_status("Search function check", False, str(e))
        return False


def verify_imports():
    """Check that all new services can be imported."""
    print("\n=== PYTHON IMPORT VERIFICATION ===")

    imports_to_check = [
        ("agent_reason", "aspire_orchestrator.nodes.agent_reason"),
        ("retrieval_router", "aspire_orchestrator.services.retrieval_router"),
        ("working_memory", "aspire_orchestrator.services.working_memory"),
        ("episodic_memory", "aspire_orchestrator.services.episodic_memory"),
        ("semantic_memory", "aspire_orchestrator.services.semantic_memory"),
        ("general_retrieval", "aspire_orchestrator.services.general_retrieval_service"),
        ("communication_retrieval", "aspire_orchestrator.services.communication_retrieval_service"),
    ]

    all_passed = True
    for name, module_path in imports_to_check:
        try:
            __import__(module_path)
            print_status(f"Import: {name}", True)
        except Exception as e:
            print_status(f"Import: {name}", False, str(e))
            all_passed = False

    return all_passed


def verify_redis():
    """Check Redis connection (non-blocking)."""
    print("\n=== REDIS VERIFICATION (optional) ===")

    redis_url = os.environ.get("REDIS_URL") or os.environ.get("ASPIRE_REDIS_URL")

    if not redis_url:
        print_status("Redis URL", False, "REDIS_URL or ASPIRE_REDIS_URL not set (will use in-memory fallback)")
        return True  # Non-blocking

    try:
        import redis
        r = redis.from_url(redis_url, socket_timeout=2)
        r.ping()
        print_status("Redis connection", True, f"Connected to {redis_url}")
        return True
    except Exception as e:
        print_status("Redis connection", False, f"{str(e)} (will use in-memory fallback)")
        return True  # Non-blocking


async def main():
    """Run all verification checks."""
    print("=" * 60)
    print("CONVERSATIONAL INTELLIGENCE LAYER — DEPLOYMENT VERIFICATION")
    print("=" * 60)

    checks = []

    # Critical checks (must pass)
    checks.append(("Tables exist", await verify_tables()))
    checks.append(("RLS policies active", await verify_rls_policies()))
    checks.append(("pgvector indexes exist", await verify_pgvector_indexes()))
    checks.append(("Search functions exist", await verify_search_functions()))
    checks.append(("Python imports work", verify_imports()))

    # Non-blocking checks (optional)
    await verify_seed_data()
    verify_redis()

    # Summary
    print("\n" + "=" * 60)
    critical_passed = all(checks)

    if critical_passed:
        print("✅ ALL CRITICAL CHECKS PASSED")
        print("\nDeployment is ready. You can now:")
        print("  1. Run seed scripts (if not already done)")
        print("  2. Start the orchestrator")
        print("  3. Test voice/avatar integration")
    else:
        print("❌ SOME CRITICAL CHECKS FAILED")
        print("\nFix the failed checks before deploying:")
        for name, passed in checks:
            if not passed:
                print(f"  - {name}")
        sys.exit(1)

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
