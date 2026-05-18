"""Wave 5.1b-2 — verify service_brief_cache migration is well-formed.

Smoke tests on the migration file itself. Full DB-integration tests (RLS
enforcement, RPC behavior) come in Wave 5.1b-9 verification.
"""
from __future__ import annotations

import pathlib


# Resolve repo migrations dir relative to this test file:
#   backend/orchestrator/tests/test_service_brief_cache_migration.py
#   -> backend/infrastructure/supabase/migrations/
MIGRATIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "infrastructure"
    / "supabase"
    / "migrations"
)


def _migration_path() -> pathlib.Path:
    matches = sorted(MIGRATIONS_DIR.glob("*service_brief_cache*.sql"))
    assert matches, (
        f"No service_brief_cache migration found in {MIGRATIONS_DIR}"
    )
    # Pick the latest if multiple ever appear; for now there should be exactly one.
    return matches[-1]


def test_migration_file_exists() -> None:
    path = _migration_path()
    assert path.is_file()
    assert path.stat().st_size > 0


def test_migration_creates_service_brief_cache_table() -> None:
    content = _migration_path().read_text(encoding="utf-8").lower()
    assert "create table" in content
    assert "service_brief_cache" in content


def test_migration_enables_rls_and_force() -> None:
    content = _migration_path().read_text(encoding="utf-8").lower()
    assert "enable row level security" in content
    assert "force row level security" in content


def test_migration_has_tenant_scoped_rls_policies() -> None:
    content = _migration_path().read_text(encoding="utf-8").lower()
    # Mirrors office_brief_cache pattern (mig 098): app.is_member() + tenant_memberships
    assert "app.is_member(tenant_id::text)" in content
    assert "tenant_memberships" in content
    assert "public.current_suite_id()" in content


def test_migration_extends_visibility_scope_check() -> None:
    """Wave 5.1b adds 'service' to memory_objects.visibility_scope CHECK."""
    content = _migration_path().read_text(encoding="utf-8").lower()
    assert "memory_objects_visibility_scope_check" in content
    # Must add 'service' to the enum
    assert "'service'" in content
    # Must keep all prior values
    for prior in ("'office'", "'finance'", "'workflow'", "'admin'", "'restricted'"):
        assert prior in content, f"visibility_scope must still allow {prior}"


def test_migration_has_reversible_down_section() -> None:
    """DOWN section must be present (commented out — do not auto-run)."""
    content = _migration_path().read_text(encoding="utf-8")
    assert "-- DOWN" in content or "-- down" in content.lower()
    assert "-- DROP TABLE IF EXISTS public.service_brief_cache" in content


def test_migration_mirrors_office_brief_cache_shape() -> None:
    """Schema parity with office_brief_cache (mig 098): same PK, same columns."""
    content = _migration_path().read_text(encoding="utf-8").lower()
    # Composite PK
    assert "primary key (tenant_id, suite_id, office_id)" in content
    # Refresh tracking
    assert "last_built_at" in content
    assert "freshness_seq" in content
    # Roll-up counters
    for col in (
        "due_now_count",
        "overdue_count",
        "pending_approval_count",
        "recent_receipts_count",
    ):
        assert col in content, f"missing roll-up column: {col}"
    # Brief content
    assert "brief_text" in content
    assert "brief_json" in content
    # Staleness index
    assert "idx_service_brief_cache_staleness" in content
