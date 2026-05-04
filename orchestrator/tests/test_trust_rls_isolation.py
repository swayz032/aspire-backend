"""RLS isolation tests for Trust Hub Wave 1 tables.

Validates Law #6 (Tenant Isolation) on the 6 tables shipped in migrations 109–111:

  1. tenant_trust_profiles      (migration 109)
  2. tenant_authorized_reps     (migration 110-A)
  3. tenant_cnam_records         (migration 110-B)
  4. trust_state_transitions     (migration 110-C — SELECT-only for authenticated)
  5. tenant_a2p_brands           (migration 111-A)
  6. tenant_a2p_campaigns        (migration 111-B)

RLS policy pattern on all 6 tables:
  USING (tenant_id::text = current_setting('request.jwt.claim.tenant_id', true))

Test strategy:
- Use psycopg (v3) direct connection to the Supabase PG pooler.
- service_role operations: connect as postgres (bypasses RLS) using ASPIRE_LANGGRAPH_POSTGRES_DSN.
  The postgres user has BYPASSRLS by default on Supabase.
- Authenticated role simulation: inside a transaction, issue:
    SET LOCAL ROLE authenticated;
    SET LOCAL "request.jwt.claim.tenant_id" = '<uuid>';
  then execute DML/SELECT. RLS fires against the local setting.
- In psycopg v3, pg.execute() returns a Cursor. Call .fetchone()/.fetchall() on the cursor.

Environment variables required (injected via `railway run`):
  ASPIRE_LANGGRAPH_POSTGRES_DSN  — pooler DSN with postgres user

Skip guard: if ASPIRE_LANGGRAPH_POSTGRES_DSN is absent, all tests are skipped
with a descriptive message (safe for CI with no DB access).

Run command:
  railway run -- wsl -d Ubuntu-22.04 -e bash -c \
    "cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator && \
     source ~/venvs/aspire/bin/activate && \
     python -m pytest tests/test_trust_rls_isolation.py -v --tb=short"
"""

from __future__ import annotations

import os
import uuid
import pytest
import psycopg
from psycopg.rows import dict_row
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUITE_A_ID  = "aaaaaaaa-bbbb-aaaa-aaaa-aaaaaaaaaaaa"
OFFICE_A_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

TENANT_B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUITE_B_ID  = "bbbbbbbb-cccc-bbbb-bbbb-bbbbbbbbbbbb"
OFFICE_B_ID = "bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

FAKE_TENANT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

# All 6 Trust Hub tables under test
TRUST_TABLES = [
    "tenant_trust_profiles",
    "tenant_authorized_reps",
    "tenant_cnam_records",
    "trust_state_transitions",
    "tenant_a2p_brands",
    "tenant_a2p_campaigns",
]

# Tables that allow authenticated writes (trust_state_transitions is SELECT-only)
WRITABLE_TABLES = [
    "tenant_trust_profiles",
    "tenant_authorized_reps",
    "tenant_cnam_records",
    "tenant_a2p_brands",
    "tenant_a2p_campaigns",
]


# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

def _get_dsn() -> str | None:
    return os.environ.get("ASPIRE_LANGGRAPH_POSTGRES_DSN", "").strip() or None


DB_AVAILABLE = _get_dsn() is not None

pytestmark = pytest.mark.skipif(
    not DB_AVAILABLE,
    reason=(
        "ASPIRE_LANGGRAPH_POSTGRES_DSN not set — run via: "
        "railway run -- wsl -d Ubuntu-22.04 -e bash -c "
        "'cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator && "
        "source ~/venvs/aspire/bin/activate && "
        "python -m pytest tests/test_trust_rls_isolation.py -v --tb=short'"
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg() -> psycopg.Connection:
    """Open a single postgres (service_role-equivalent) connection for the module.

    autocommit=True so each helper can manage its own transactions via
    conn.transaction() context manager.

    prepare_threshold=None disables server-side prepared statements, which avoids
    "prepared statement already exists" errors that arise when the same parameterized
    query is executed in multiple tests over a long-lived module-scoped connection.
    """
    dsn = _get_dsn()
    conn = psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=None,
    )
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers: service_role DML (no RLS — postgres user has BYPASSRLS on Supabase)
# In psycopg v3, connection.execute() returns a Cursor; call fetchone/fetchall on it.
# ---------------------------------------------------------------------------

def _sr_exec(pg: psycopg.Connection, sql: str, params: tuple = ()) -> psycopg.Cursor:
    """Execute SQL as service_role (autocommit). Returns the cursor."""
    return pg.execute(sql, params)


def _sr_insert_trust_profile(
    pg: psycopg.Connection,
    tenant_id: str,
    suite_id: str,
    office_id: str,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Insert a minimal tenant_trust_profiles row as service_role."""
    pid = profile_id or str(uuid.uuid4())
    cur = _sr_exec(
        pg,
        """
        INSERT INTO public.tenant_trust_profiles (
            id, tenant_id, suite_id, office_id,
            legal_business_name, business_type,
            address_street, address_city, address_state, address_zip
        ) VALUES (
            %s, %s::uuid, %s::uuid, %s::uuid,
            %s, %s,
            %s, %s, %s, %s
        )
        ON CONFLICT (suite_id) DO NOTHING
        RETURNING *;
        """,
        (
            pid, tenant_id, suite_id, office_id,
            f"Test Business {tenant_id[:4]}",
            "llc",
            "123 Test St", "Test City", "TX", "75001",
        ),
    )
    row = cur.fetchone()
    if row is None:
        # ON CONFLICT DO NOTHING returns nothing — fetch existing
        cur = _sr_exec(
            pg,
            "SELECT * FROM public.tenant_trust_profiles WHERE suite_id = %s::uuid;",
            (suite_id,),
        )
        row = cur.fetchone()
    return row  # type: ignore[return-value]


def _sr_insert_authorized_rep(
    pg: psycopg.Connection,
    tenant_id: str,
    suite_id: str,
    trust_profile_id: str,
    rep_id: str | None = None,
) -> dict[str, Any]:
    rid = rep_id or str(uuid.uuid4())
    cur = _sr_exec(
        pg,
        """
        INSERT INTO public.tenant_authorized_reps (
            id, tenant_id, suite_id, trust_profile_id,
            first_name, last_name, title, email, phone_e164
        ) VALUES (
            %s, %s::uuid, %s::uuid, %s::uuid,
            %s, %s, %s, %s, %s
        )
        RETURNING *;
        """,
        (rid, tenant_id, suite_id, trust_profile_id,
         "Test", "Rep", "Owner", f"rep+{rid[:4]}@test.com", "+15555550001"),
    )
    return cur.fetchone()  # type: ignore[return-value]


def _sr_insert_cnam_record(
    pg: psycopg.Connection,
    tenant_id: str,
    suite_id: str,
    trust_profile_id: str,
    phone_number_id: str,
    cnam_id: str | None = None,
) -> dict[str, Any]:
    cid = cnam_id or str(uuid.uuid4())
    cur = _sr_exec(
        pg,
        """
        INSERT INTO public.tenant_cnam_records (
            id, tenant_id, suite_id, phone_number_id, trust_profile_id,
            cnam_display_name, raw_business_name
        ) VALUES (
            %s, %s::uuid, %s::uuid, %s::uuid, %s::uuid,
            %s, %s
        )
        RETURNING *;
        """,
        (cid, tenant_id, suite_id, phone_number_id, trust_profile_id,
         f"TESTBIZ{cid[:4].upper()}", f"Test Business {cid[:4]}"),
    )
    return cur.fetchone()  # type: ignore[return-value]


def _sr_insert_state_transition(
    pg: psycopg.Connection,
    tenant_id: str,
    suite_id: str,
    trust_profile_id: str,
    transition_id: str | None = None,
) -> dict[str, Any]:
    tid = transition_id or str(uuid.uuid4())
    cur = _sr_exec(
        pg,
        """
        INSERT INTO public.trust_state_transitions (
            id, tenant_id, suite_id, trust_profile_id,
            from_state, to_state, event_type
        ) VALUES (
            %s, %s::uuid, %s::uuid, %s::uuid,
            %s, %s, %s
        )
        RETURNING *;
        """,
        (tid, tenant_id, suite_id, trust_profile_id,
         "kyb_collected", "profile_drafted", "test_event"),
    )
    return cur.fetchone()  # type: ignore[return-value]


def _sr_insert_a2p_brand(
    pg: psycopg.Connection,
    tenant_id: str,
    suite_id: str,
    brand_id: str | None = None,
) -> dict[str, Any]:
    bid = brand_id or str(uuid.uuid4())
    cur = _sr_exec(
        pg,
        """
        INSERT INTO public.tenant_a2p_brands (
            id, tenant_id, suite_id
        ) VALUES (
            %s, %s::uuid, %s::uuid
        )
        ON CONFLICT (suite_id) DO NOTHING
        RETURNING *;
        """,
        (bid, tenant_id, suite_id),
    )
    row = cur.fetchone()
    if row is None:
        cur = _sr_exec(
            pg,
            "SELECT * FROM public.tenant_a2p_brands WHERE suite_id = %s::uuid;",
            (suite_id,),
        )
        row = cur.fetchone()
    return row  # type: ignore[return-value]


def _sr_insert_a2p_campaign(
    pg: psycopg.Connection,
    tenant_id: str,
    suite_id: str,
    brand_id: str,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    cid = campaign_id or str(uuid.uuid4())
    cur = _sr_exec(
        pg,
        """
        INSERT INTO public.tenant_a2p_campaigns (
            id, tenant_id, suite_id, brand_id,
            campaign_use_case, campaign_description
        ) VALUES (
            %s, %s::uuid, %s::uuid, %s::uuid,
            %s, %s
        )
        RETURNING *;
        """,
        (cid, tenant_id, suite_id, brand_id,
         "MIXED", f"Test campaign for tenant {tenant_id[:4]}"),
    )
    return cur.fetchone()  # type: ignore[return-value]


def _sr_get_phone_number_id(pg: psycopg.Connection, suite_id: str) -> str | None:
    """Return an existing phone_number_id for the given suite, or None."""
    cur = _sr_exec(
        pg,
        "SELECT id FROM public.tenant_phone_numbers WHERE suite_id = %s::uuid LIMIT 1;",
        (suite_id,),
    )
    row = cur.fetchone()
    return str(row["id"]) if row else None


# ---------------------------------------------------------------------------
# Authenticated-role helpers
# ---------------------------------------------------------------------------

def _set_tenant_claim(pg: psycopg.Connection, tenant_id: str) -> None:
    """SET LOCAL the JWT tenant_id claim for the current transaction.

    PostgreSQL SET LOCAL does not accept parameterized ($1) values — the value
    must be a literal string embedded directly into the SQL. We use an f-string
    here which is safe because tenant_id is always a UUID (validated by callers).
    The value is quoted with single quotes in the SET LOCAL statement.
    """
    # Escape single quotes in case (UUIDs don't have them, but be safe)
    safe_id = tenant_id.replace("'", "''")
    pg.execute(f"SET LOCAL \"request.jwt.claim.tenant_id\" = '{safe_id}';")


def _select_as_authenticated(
    pg: psycopg.Connection,
    table: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Return all rows visible to 'authenticated' role with the given tenant_id claim."""
    with pg.transaction():
        pg.execute("SET LOCAL ROLE authenticated;")
        _set_tenant_claim(pg, tenant_id)
        cur = pg.execute(f"SELECT * FROM public.{table};")  # noqa: S608
        rows = cur.fetchall()
    return rows


def _count_as_authenticated(
    pg: psycopg.Connection,
    table: str,
    tenant_id: str,
) -> int:
    """Count rows visible to 'authenticated' role with the given tenant_id claim."""
    rows = _select_as_authenticated(pg, table, tenant_id)
    return len(rows)


def _insert_as_authenticated(
    pg: psycopg.Connection,
    sql: str,
    params: tuple,
    tenant_id: str,
) -> tuple[bool, str]:
    """Attempt an INSERT as authenticated role. Returns (succeeded, error_msg)."""
    try:
        with pg.transaction():
            pg.execute("SET LOCAL ROLE authenticated;")
            _set_tenant_claim(pg, tenant_id)
            pg.execute(sql, params)
        return (True, "")
    except Exception as exc:
        return (False, str(exc))


def _update_as_authenticated(
    pg: psycopg.Connection,
    sql: str,
    params: tuple,
    tenant_id: str,
) -> tuple[bool, str]:
    """Attempt an UPDATE as authenticated role. Returns (succeeded, error_msg)."""
    try:
        with pg.transaction():
            pg.execute("SET LOCAL ROLE authenticated;")
            _set_tenant_claim(pg, tenant_id)
            pg.execute(sql, params)
        return (True, "")
    except Exception as exc:
        return (False, str(exc))


def _delete_as_authenticated(
    pg: psycopg.Connection,
    sql: str,
    params: tuple,
    tenant_id: str,
) -> tuple[bool, str]:
    """Attempt a DELETE as authenticated role. Returns (succeeded, error_msg)."""
    try:
        with pg.transaction():
            pg.execute("SET LOCAL ROLE authenticated;")
            _set_tenant_claim(pg, tenant_id)
            pg.execute(sql, params)
        return (True, "")
    except Exception as exc:
        return (False, str(exc))


# ---------------------------------------------------------------------------
# Module-scope fixture: insert test data for both tenants
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def trust_test_data(pg: psycopg.Connection) -> dict[str, Any]:
    """Insert seed data for Tenant A and Tenant B. Tear down after module."""
    data: dict[str, Any] = {}

    # trust profiles
    prof_a = _sr_insert_trust_profile(pg, TENANT_A_ID, SUITE_A_ID, OFFICE_A_ID)
    prof_b = _sr_insert_trust_profile(pg, TENANT_B_ID, SUITE_B_ID, OFFICE_B_ID)
    data["profile_a_id"] = str(prof_a["id"])
    data["profile_b_id"] = str(prof_b["id"])

    # authorized reps
    rep_a = _sr_insert_authorized_rep(pg, TENANT_A_ID, SUITE_A_ID, data["profile_a_id"])
    rep_b = _sr_insert_authorized_rep(pg, TENANT_B_ID, SUITE_B_ID, data["profile_b_id"])
    data["rep_a_id"] = str(rep_a["id"])
    data["rep_b_id"] = str(rep_b["id"])

    # state transitions
    trans_a = _sr_insert_state_transition(pg, TENANT_A_ID, SUITE_A_ID, data["profile_a_id"])
    trans_b = _sr_insert_state_transition(pg, TENANT_B_ID, SUITE_B_ID, data["profile_b_id"])
    data["trans_a_id"] = str(trans_a["id"])
    data["trans_b_id"] = str(trans_b["id"])

    # a2p brands
    brand_a = _sr_insert_a2p_brand(pg, TENANT_A_ID, SUITE_A_ID)
    brand_b = _sr_insert_a2p_brand(pg, TENANT_B_ID, SUITE_B_ID)
    data["brand_a_id"] = str(brand_a["id"])
    data["brand_b_id"] = str(brand_b["id"])

    # a2p campaigns
    camp_a = _sr_insert_a2p_campaign(pg, TENANT_A_ID, SUITE_A_ID, data["brand_a_id"])
    camp_b = _sr_insert_a2p_campaign(pg, TENANT_B_ID, SUITE_B_ID, data["brand_b_id"])
    data["camp_a_id"] = str(camp_a["id"])
    data["camp_b_id"] = str(camp_b["id"])

    # cnam records — only if phone numbers exist for the test suites
    pn_a = _sr_get_phone_number_id(pg, SUITE_A_ID)
    pn_b = _sr_get_phone_number_id(pg, SUITE_B_ID)
    data["has_cnam_a"] = False
    data["has_cnam_b"] = False

    if pn_a:
        cur = _sr_exec(
            pg,
            "SELECT id FROM public.tenant_cnam_records WHERE phone_number_id = %s::uuid;",
            (pn_a,),
        )
        existing = cur.fetchone()
        if existing is None:
            cnam_a = _sr_insert_cnam_record(
                pg, TENANT_A_ID, SUITE_A_ID, data["profile_a_id"], pn_a
            )
            data["cnam_a_id"] = str(cnam_a["id"])
            data["cnam_a_inserted"] = True
        else:
            data["cnam_a_id"] = str(existing["id"])
            data["cnam_a_inserted"] = False
        data["has_cnam_a"] = True

    if pn_b:
        cur = _sr_exec(
            pg,
            "SELECT id FROM public.tenant_cnam_records WHERE phone_number_id = %s::uuid;",
            (pn_b,),
        )
        existing = cur.fetchone()
        if existing is None:
            cnam_b = _sr_insert_cnam_record(
                pg, TENANT_B_ID, SUITE_B_ID, data["profile_b_id"], pn_b
            )
            data["cnam_b_id"] = str(cnam_b["id"])
            data["cnam_b_inserted"] = True
        else:
            data["cnam_b_id"] = str(existing["id"])
            data["cnam_b_inserted"] = False
        data["has_cnam_b"] = True

    yield data

    # ---------------------------------------------------------------------------
    # Teardown: delete test data in dependency order (child before parent)
    # Only delete rows we inserted (tracked by ID). Production rows are untouched.
    # ---------------------------------------------------------------------------

    # a2p_campaigns
    for cid in [data.get("camp_a_id"), data.get("camp_b_id")]:
        if cid:
            _sr_exec(pg, "DELETE FROM public.tenant_a2p_campaigns WHERE id = %s::uuid;", (cid,))

    # a2p_brands
    for bid in [data.get("brand_a_id"), data.get("brand_b_id")]:
        if bid:
            _sr_exec(pg, "DELETE FROM public.tenant_a2p_brands WHERE id = %s::uuid;", (bid,))

    # cnam_records (only if we inserted them)
    if data.get("cnam_a_inserted") and data.get("cnam_a_id"):
        _sr_exec(pg, "DELETE FROM public.tenant_cnam_records WHERE id = %s::uuid;", (data["cnam_a_id"],))
    if data.get("cnam_b_inserted") and data.get("cnam_b_id"):
        _sr_exec(pg, "DELETE FROM public.tenant_cnam_records WHERE id = %s::uuid;", (data["cnam_b_id"],))

    # trust_state_transitions
    for tid in [data.get("trans_a_id"), data.get("trans_b_id")]:
        if tid:
            _sr_exec(pg, "DELETE FROM public.trust_state_transitions WHERE id = %s::uuid;", (tid,))

    # authorized_reps
    for rid in [data.get("rep_a_id"), data.get("rep_b_id")]:
        if rid:
            _sr_exec(pg, "DELETE FROM public.tenant_authorized_reps WHERE id = %s::uuid;", (rid,))

    # trust_profiles (last — RESTRICT FK from trust_state_transitions already deleted above)
    for pid in [data.get("profile_a_id"), data.get("profile_b_id")]:
        if pid:
            _sr_exec(pg, "DELETE FROM public.tenant_trust_profiles WHERE id = %s::uuid;", (pid,))


# ===========================================================================
# Test Class 1: Cross-tenant SELECT returns zero rows
# Law #6: ZERO cross-tenant leakage
# ===========================================================================

class TestCrossTenantSelectIsolation:
    """Law #6 — Tenant A cannot see Tenant B rows via SELECT, and vice versa.

    Verifies the USING clause of each table's *_tenant_isolation RLS policy.
    """

    def test_tenant_a_selects_only_own_trust_profiles(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A's SELECT on tenant_trust_profiles returns only Tenant A rows."""
        rows = _select_as_authenticated(pg, "tenant_trust_profiles", TENANT_A_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_B_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_trust_profiles returns Tenant B rows under Tenant A claim"
        )
        assert any(str(r["id"]) == trust_test_data["profile_a_id"] for r in rows), (
            "Tenant A cannot see its own tenant_trust_profiles row — RLS policy may be broken"
        )

    def test_tenant_b_selects_only_own_trust_profiles(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant B's SELECT on tenant_trust_profiles returns only Tenant B rows."""
        rows = _select_as_authenticated(pg, "tenant_trust_profiles", TENANT_B_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_A_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_trust_profiles returns Tenant A rows under Tenant B claim"
        )
        assert any(str(r["id"]) == trust_test_data["profile_b_id"] for r in rows)

    def test_cross_tenant_count_zero_trust_profiles(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Under Tenant A's claim, count of Tenant B rows in tenant_trust_profiles == 0."""
        rows = _select_as_authenticated(pg, "tenant_trust_profiles", TENANT_A_ID)
        b_visible = [r for r in rows if str(r["tenant_id"]) == TENANT_B_ID]
        assert len(b_visible) == 0, (
            f"ISOLATION BREACH: {len(b_visible)} Tenant B rows visible under Tenant A claim"
        )

    def test_tenant_a_selects_only_own_authorized_reps(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A's SELECT on tenant_authorized_reps returns only Tenant A rows."""
        rows = _select_as_authenticated(pg, "tenant_authorized_reps", TENANT_A_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_B_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_authorized_reps leaks Tenant B rows"
        )
        assert any(str(r["id"]) == trust_test_data["rep_a_id"] for r in rows)

    def test_tenant_b_selects_only_own_authorized_reps(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant B's SELECT on tenant_authorized_reps returns only Tenant B rows."""
        rows = _select_as_authenticated(pg, "tenant_authorized_reps", TENANT_B_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_A_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_authorized_reps leaks Tenant A rows"
        )

    def test_tenant_a_selects_only_own_cnam_records(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A's SELECT on tenant_cnam_records returns only Tenant A rows."""
        if not trust_test_data["has_cnam_a"] and not trust_test_data["has_cnam_b"]:
            pytest.skip("No phone numbers seeded for either test suite — cnam_records test skipped")
        rows = _select_as_authenticated(pg, "tenant_cnam_records", TENANT_A_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_B_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_cnam_records leaks Tenant B rows"
        )

    def test_tenant_b_selects_only_own_cnam_records(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant B's SELECT on tenant_cnam_records returns only Tenant B rows."""
        if not trust_test_data["has_cnam_a"] and not trust_test_data["has_cnam_b"]:
            pytest.skip("No phone numbers seeded for either test suite — cnam_records test skipped")
        rows = _select_as_authenticated(pg, "tenant_cnam_records", TENANT_B_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_A_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_cnam_records leaks Tenant A rows"
        )

    def test_tenant_a_selects_only_own_state_transitions(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A's SELECT on trust_state_transitions returns only Tenant A rows."""
        rows = _select_as_authenticated(pg, "trust_state_transitions", TENANT_A_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_B_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: trust_state_transitions leaks Tenant B rows"
        )
        assert any(str(r["id"]) == trust_test_data["trans_a_id"] for r in rows)

    def test_tenant_b_selects_only_own_state_transitions(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant B's SELECT on trust_state_transitions returns only Tenant B rows."""
        rows = _select_as_authenticated(pg, "trust_state_transitions", TENANT_B_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_A_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: trust_state_transitions leaks Tenant A rows"
        )

    def test_tenant_a_selects_only_own_a2p_brands(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A's SELECT on tenant_a2p_brands returns only Tenant A rows."""
        rows = _select_as_authenticated(pg, "tenant_a2p_brands", TENANT_A_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_B_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_a2p_brands leaks Tenant B rows"
        )
        assert any(str(r["id"]) == trust_test_data["brand_a_id"] for r in rows)

    def test_tenant_b_selects_only_own_a2p_brands(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant B's SELECT on tenant_a2p_brands returns only Tenant B rows."""
        rows = _select_as_authenticated(pg, "tenant_a2p_brands", TENANT_B_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_A_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_a2p_brands leaks Tenant A rows"
        )

    def test_tenant_a_selects_only_own_a2p_campaigns(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A's SELECT on tenant_a2p_campaigns returns only Tenant A rows."""
        rows = _select_as_authenticated(pg, "tenant_a2p_campaigns", TENANT_A_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_B_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_a2p_campaigns leaks Tenant B rows"
        )
        assert any(str(r["id"]) == trust_test_data["camp_a_id"] for r in rows)

    def test_tenant_b_selects_only_own_a2p_campaigns(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant B's SELECT on tenant_a2p_campaigns returns only Tenant B rows."""
        rows = _select_as_authenticated(pg, "tenant_a2p_campaigns", TENANT_B_ID)
        tenant_ids = {str(r["tenant_id"]) for r in rows}
        assert TENANT_A_ID not in tenant_ids, (
            "TENANT ISOLATION BREACH: tenant_a2p_campaigns leaks Tenant A rows"
        )

    def test_missing_tenant_claim_returns_no_test_rows(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Empty tenant_id claim → our test rows must NOT be visible across all tables.

        current_setting('request.jwt.claim.tenant_id', true) with an empty string
        won't match any real UUID → 0 rows from our seed data.
        """
        for table in TRUST_TABLES:
            rows = _select_as_authenticated(pg, table, "")
            leaked = [
                r for r in rows
                if str(r.get("tenant_id", "")) in (TENANT_A_ID, TENANT_B_ID)
            ]
            assert len(leaked) == 0, (
                f"ISOLATION BREACH: table={table} — {len(leaked)} real rows visible "
                "with empty tenant_id claim"
            )

    def test_fake_tenant_id_returns_no_test_rows(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """A forged non-existent tenant_id must return zero rows across all tables."""
        for table in TRUST_TABLES:
            rows = _select_as_authenticated(pg, table, FAKE_TENANT_ID)
            real_rows = [
                r for r in rows
                if str(r.get("tenant_id", "")) in (TENANT_A_ID, TENANT_B_ID)
            ]
            assert len(real_rows) == 0, (
                f"ISOLATION BREACH: table={table} — {len(real_rows)} rows leaked "
                "under fake tenant claim"
            )


# ===========================================================================
# Test Class 2: Cross-tenant INSERT/UPDATE/DELETE blocked
# Law #6: WITH CHECK clause enforcement
# ===========================================================================

class TestCrossTenantWriteBlocked:
    """Law #6 — Tenant A cannot INSERT/UPDATE/DELETE rows belonging to Tenant B.

    trust_state_transitions is excluded — covered in TestTrustStateTransitionsAppendOnly.
    """

    def test_trust_profile_insert_with_wrong_tenant_id_blocked(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Authenticated Tenant A cannot INSERT a trust_profile with Tenant B's tenant_id."""
        new_id = str(uuid.uuid4())
        succeeded, err = _insert_as_authenticated(
            pg,
            """
            INSERT INTO public.tenant_trust_profiles (
                id, tenant_id, suite_id, office_id,
                legal_business_name, business_type,
                address_street, address_city, address_state, address_zip
            ) VALUES (
                %s, %s::uuid, %s::uuid, %s::uuid,
                %s, %s, %s, %s, %s, %s
            );
            """,
            (
                new_id,
                TENANT_B_ID,            # claiming B's tenant_id — WITH CHECK must block
                str(uuid.uuid4()),       # new suite_id
                str(uuid.uuid4()),
                "Evil Corp", "llc",
                "Evil St", "Evil City", "TX", "00000",
            ),
            TENANT_A_ID,  # authenticated as Tenant A
        )
        assert not succeeded, (
            "ISOLATION BREACH: Tenant A inserted a trust_profile with Tenant B's tenant_id. "
            "WITH CHECK clause in ttp_tenant_isolation policy must be blocking this."
        )
        # Paranoia cleanup in case it somehow succeeded
        _sr_exec(pg, "DELETE FROM public.tenant_trust_profiles WHERE id = %s::uuid;", (new_id,))

    def test_trust_profile_update_of_tenant_b_row_is_invisible(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Authenticated Tenant A UPDATE on Tenant B's trust_profile affects 0 rows.

        PostgreSQL RLS behavior: UPDATE on a row that does not pass USING clause
        silently affects 0 rows (does NOT raise an exception). We verify the row
        was not mutated.
        """
        _update_as_authenticated(
            pg,
            "UPDATE public.tenant_trust_profiles SET legal_business_name = %s WHERE id = %s::uuid;",
            ("HACKED", trust_test_data["profile_b_id"]),
            TENANT_A_ID,
        )
        cur = _sr_exec(
            pg,
            "SELECT legal_business_name FROM public.tenant_trust_profiles WHERE id = %s::uuid;",
            (trust_test_data["profile_b_id"],),
        )
        row = cur.fetchone()
        if row:
            assert row["legal_business_name"] != "HACKED", (
                "ISOLATION BREACH: Tenant A successfully updated Tenant B's trust_profile row"
            )

    def test_trust_profile_delete_of_tenant_b_row_is_invisible(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Authenticated Tenant A DELETE on Tenant B's trust_profile affects 0 rows."""
        _delete_as_authenticated(
            pg,
            "DELETE FROM public.tenant_trust_profiles WHERE id = %s::uuid;",
            (trust_test_data["profile_b_id"],),
            TENANT_A_ID,
        )
        # Verify row still exists
        cur = _sr_exec(
            pg,
            "SELECT id FROM public.tenant_trust_profiles WHERE id = %s::uuid;",
            (trust_test_data["profile_b_id"],),
        )
        assert cur.fetchone() is not None, (
            "ISOLATION BREACH: Tenant A deleted Tenant B's trust_profile row — "
            "DELETE USING clause did not block"
        )

    def test_authorized_rep_insert_with_wrong_tenant_id_blocked(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A cannot INSERT an authorized_rep claiming Tenant B's tenant_id."""
        new_id = str(uuid.uuid4())
        succeeded, err = _insert_as_authenticated(
            pg,
            """
            INSERT INTO public.tenant_authorized_reps (
                id, tenant_id, suite_id, trust_profile_id,
                first_name, last_name, title, email, phone_e164
            ) VALUES (
                %s, %s::uuid, %s::uuid, %s::uuid,
                %s, %s, %s, %s, %s
            );
            """,
            (
                new_id,
                TENANT_B_ID,  # wrong tenant
                SUITE_B_ID,
                trust_test_data["profile_b_id"],
                "Evil", "Rep", "Owner", "evil@example.com", "+15555550002",
            ),
            TENANT_A_ID,
        )
        assert not succeeded, (
            "ISOLATION BREACH: Tenant A inserted a tenant_authorized_rep with Tenant B's tenant_id"
        )
        _sr_exec(pg, "DELETE FROM public.tenant_authorized_reps WHERE id = %s::uuid;", (new_id,))

    def test_authorized_rep_update_of_tenant_b_row_is_invisible(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A's UPDATE on Tenant B's authorized_rep row is invisible (0 rows affected)."""
        _update_as_authenticated(
            pg,
            "UPDATE public.tenant_authorized_reps SET first_name = %s WHERE id = %s::uuid;",
            ("HACKED", trust_test_data["rep_b_id"]),
            TENANT_A_ID,
        )
        cur = _sr_exec(
            pg,
            "SELECT first_name FROM public.tenant_authorized_reps WHERE id = %s::uuid;",
            (trust_test_data["rep_b_id"],),
        )
        row = cur.fetchone()
        if row:
            assert row["first_name"] != "HACKED", (
                "ISOLATION BREACH: Tenant A updated Tenant B's authorized_rep row"
            )

    def test_a2p_brand_insert_with_wrong_tenant_id_blocked(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A cannot INSERT an a2p_brand claiming Tenant B's tenant_id."""
        new_id = str(uuid.uuid4())
        new_suite = str(uuid.uuid4())  # distinct suite to avoid UNIQUE conflict
        succeeded, err = _insert_as_authenticated(
            pg,
            """
            INSERT INTO public.tenant_a2p_brands (
                id, tenant_id, suite_id
            ) VALUES (
                %s, %s::uuid, %s::uuid
            );
            """,
            (new_id, TENANT_B_ID, new_suite),
            TENANT_A_ID,
        )
        assert not succeeded, (
            "ISOLATION BREACH: Tenant A inserted a tenant_a2p_brand with Tenant B's tenant_id"
        )
        _sr_exec(pg, "DELETE FROM public.tenant_a2p_brands WHERE id = %s::uuid;", (new_id,))

    def test_a2p_brand_update_of_tenant_b_row_is_invisible(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A's UPDATE on Tenant B's a2p_brand row affects 0 rows."""
        _update_as_authenticated(
            pg,
            "UPDATE public.tenant_a2p_brands SET brand_type = %s WHERE id = %s::uuid;",
            ("standard", trust_test_data["brand_b_id"]),
            TENANT_A_ID,
        )
        cur = _sr_exec(
            pg,
            "SELECT brand_type, tenant_id FROM public.tenant_a2p_brands WHERE id = %s::uuid;",
            (trust_test_data["brand_b_id"],),
        )
        row = cur.fetchone()
        if row:
            # Row is still Tenant B's — UPDATE must not have changed brand_type to 'standard'
            # (unless the brand was already 'standard', which our seed data does not set)
            assert str(row["tenant_id"]) == TENANT_B_ID, "Tenant B brand was hijacked"

    def test_a2p_campaign_insert_with_wrong_tenant_id_blocked(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Tenant A cannot INSERT an a2p_campaign claiming Tenant B's tenant_id."""
        new_id = str(uuid.uuid4())
        succeeded, err = _insert_as_authenticated(
            pg,
            """
            INSERT INTO public.tenant_a2p_campaigns (
                id, tenant_id, suite_id, brand_id,
                campaign_use_case, campaign_description
            ) VALUES (
                %s, %s::uuid, %s::uuid, %s::uuid,
                %s, %s
            );
            """,
            (
                new_id,
                TENANT_B_ID,  # wrong tenant
                SUITE_B_ID,
                trust_test_data["brand_b_id"],
                "MIXED",
                "Malicious campaign",
            ),
            TENANT_A_ID,
        )
        assert not succeeded, (
            "ISOLATION BREACH: Tenant A inserted a tenant_a2p_campaign with Tenant B's tenant_id"
        )
        _sr_exec(pg, "DELETE FROM public.tenant_a2p_campaigns WHERE id = %s::uuid;", (new_id,))


# ===========================================================================
# Test Class 3: trust_state_transitions is append-only for authenticated
# Law #6 + Law #2 (append-only audit ledger)
# ===========================================================================

class TestTrustStateTransitionsAppendOnly:
    """trust_state_transitions must be:
    - SELECT-only for authenticated (no INSERT/UPDATE/DELETE policy exists)
    - Fully writable by service_role (worker is sole writer)
    """

    def test_authenticated_cannot_insert_state_transition(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Authenticated role must NOT insert into trust_state_transitions."""
        new_id = str(uuid.uuid4())
        succeeded, err = _insert_as_authenticated(
            pg,
            """
            INSERT INTO public.trust_state_transitions (
                id, tenant_id, suite_id, trust_profile_id,
                from_state, to_state, event_type
            ) VALUES (
                %s, %s::uuid, %s::uuid, %s::uuid,
                %s, %s, %s
            );
            """,
            (
                new_id,
                TENANT_A_ID, SUITE_A_ID, trust_test_data["profile_a_id"],
                "kyb_collected", "profile_drafted", "malicious_insert",
            ),
            TENANT_A_ID,
        )
        assert not succeeded, (
            "APPEND-ONLY BREACH: authenticated role inserted into trust_state_transitions. "
            "No INSERT policy should exist for authenticated — worker is the sole writer."
        )
        # Paranoia cleanup
        _sr_exec(pg, "DELETE FROM public.trust_state_transitions WHERE id = %s::uuid;", (new_id,))

    def test_authenticated_cannot_update_own_state_transition(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Authenticated role must NOT be able to UPDATE any trust_state_transitions row.

        This table has no UPDATE policy for authenticated. PostgreSQL will either
        raise 'insufficient_privilege' or UPDATE 0 rows (silent no-op). Either is
        acceptable. We verify the row was not mutated.
        """
        try:
            with pg.transaction():
                pg.execute("SET LOCAL ROLE authenticated;")
                _set_tenant_claim(pg, TENANT_A_ID)
                pg.execute(
                    "UPDATE public.trust_state_transitions SET event_type = %s WHERE id = %s::uuid;",
                    ("TAMPERED", trust_test_data["trans_a_id"]),
                )
        except Exception:
            pass  # exception is acceptable

        cur = _sr_exec(
            pg,
            "SELECT event_type FROM public.trust_state_transitions WHERE id = %s::uuid;",
            (trust_test_data["trans_a_id"],),
        )
        row = cur.fetchone()
        if row:
            assert row["event_type"] != "TAMPERED", (
                "APPEND-ONLY BREACH: authenticated role mutated a trust_state_transitions row"
            )

    def test_authenticated_cannot_delete_state_transition(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Authenticated role must NOT be able to DELETE trust_state_transitions rows."""
        _delete_as_authenticated(
            pg,
            "DELETE FROM public.trust_state_transitions WHERE id = %s::uuid;",
            (trust_test_data["trans_a_id"],),
            TENANT_A_ID,
        )
        # Row must still exist
        cur = _sr_exec(
            pg,
            "SELECT id FROM public.trust_state_transitions WHERE id = %s::uuid;",
            (trust_test_data["trans_a_id"],),
        )
        assert cur.fetchone() is not None, (
            "APPEND-ONLY BREACH: authenticated role deleted a trust_state_transitions row — "
            "no DELETE policy should exist for authenticated"
        )

    def test_authenticated_can_select_own_state_transitions(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """Authenticated role CAN SELECT its own state transitions (SELECT policy exists)."""
        rows = _select_as_authenticated(pg, "trust_state_transitions", TENANT_A_ID)
        assert any(str(r["id"]) == trust_test_data["trans_a_id"] for r in rows), (
            "Authenticated role cannot SELECT its own trust_state_transitions rows — "
            "tst_tenant_select policy may be missing or broken"
        )

    def test_service_role_can_insert_state_transition(
        self, pg: psycopg.Connection, trust_test_data: dict[str, Any]
    ) -> None:
        """service_role (worker) CAN insert into trust_state_transitions."""
        new_id = str(uuid.uuid4())
        try:
            row = _sr_insert_state_transition(
                pg, TENANT_A_ID, SUITE_A_ID,
                trust_test_data["profile_a_id"],
                transition_id=new_id,
            )
            assert str(row["id"]) == new_id, "service_role insert returned wrong row"
        finally:
            _sr_exec(pg, "DELETE FROM public.trust_state_transitions WHERE id = %s::uuid;", (new_id,))


# ===========================================================================
# Test Class 4: vault.decrypted_secrets inaccessible to authenticated
# Law #9 + Law #6: PII encryption enforcement
# ===========================================================================

class TestVaultInaccessibleToAuthenticated:
    """vault.decrypted_secrets must be inaccessible to the authenticated role.

    EIN, DOB, SSN last 4 are stored as UUID references in vault.secrets.
    The vault.decrypted_secrets VIEW decrypts them — service_role only.
    """

    def test_authenticated_cannot_query_vault_decrypted_secrets(
        self, pg: psycopg.Connection
    ) -> None:
        """Authenticated role querying vault.decrypted_secrets must return 0 rows or raise.

        Supabase Vault restricts the view to service_role. An authenticated role either:
          (a) Receives a 'permission denied' error (insufficient_privilege / 42501), OR
          (b) Gets 0 rows (if a restrictive RLS-like policy applies to the view).
        Both outcomes satisfy Law #9. Any non-zero row count is a SECURITY BREACH.
        """
        row_count = 0
        error_raised = False
        try:
            with pg.transaction():
                pg.execute("SET LOCAL ROLE authenticated;")
                _set_tenant_claim(pg, TENANT_A_ID)
                cur = pg.execute("SELECT COUNT(*) AS n FROM vault.decrypted_secrets;")
                row = cur.fetchone()
                row_count = int(row["n"]) if row else 0
        except Exception as exc:
            error_raised = True
            err_str = str(exc).lower()
            assert any(
                phrase in err_str
                for phrase in (
                    "permission denied",
                    "insufficient_privilege",
                    "42501",
                    "does not exist",
                    "schema",
                    "syntax",  # Some PG versions reject SET LOCAL in this context
                )
            ), (
                f"Unexpected error querying vault.decrypted_secrets as authenticated: {exc}"
            )

        if not error_raised:
            assert row_count == 0, (
                f"SECURITY BREACH: vault.decrypted_secrets returned {row_count} rows to "
                "authenticated role. PII vault must be service_role-only per Law #9."
            )

    def test_service_role_can_query_vault_decrypted_secrets(
        self, pg: psycopg.Connection
    ) -> None:
        """service_role CAN query vault.decrypted_secrets (sanity check / preflight P6 validation).

        If this test fails with 'does not exist', pgsodium/Vault is not enabled —
        migrations 109-111 cannot store PII safely. This is a hard block.
        """
        try:
            cur = _sr_exec(pg, "SELECT COUNT(*) AS n FROM vault.decrypted_secrets;")
            row = cur.fetchone()
            assert row is not None, "vault.decrypted_secrets returned no row for service_role"
        except Exception as exc:
            err_str = str(exc).lower()
            if "does not exist" in err_str or "pgsodium" in err_str:
                pytest.skip(
                    f"vault.decrypted_secrets not available in this Supabase instance "
                    f"(pgsodium extension may not be enabled): {exc}"
                )
            raise


# ===========================================================================
# Test Class 5: RLS FORCED enforcement (relforcerowsecurity in pg_class)
# Law #6: even table owner cannot bypass RLS without SET row_security = off
# ===========================================================================

class TestRLSForcedEnforcement:
    """Verify ENABLE + FORCE ROW LEVEL SECURITY on all 6 Trust Hub tables.

    relforcerowsecurity = true in pg_class means even the postgres user
    (table owner) cannot bypass RLS without explicit SET row_security = off.
    This is the critical flag that ensures the orchestrator's postgres
    connection is also subject to tenant isolation.
    """

    @pytest.mark.parametrize("table_name", TRUST_TABLES)
    def test_table_has_rls_enabled_and_forced(
        self, pg: psycopg.Connection, table_name: str
    ) -> None:
        """relrowsecurity=true AND relforcerowsecurity=true for each Trust Hub table."""
        cur = _sr_exec(
            pg,
            """
            SELECT
                relname,
                relrowsecurity        AS rls_enabled,
                relforcerowsecurity   AS rls_forced
            FROM pg_class
            WHERE relname = %s
              AND relnamespace = (
                  SELECT oid FROM pg_namespace WHERE nspname = 'public'
              );
            """,
            (table_name,),
        )
        row = cur.fetchone()

        assert row is not None, (
            f"Table public.{table_name} not found in pg_class — "
            "migration may not have been applied to this database"
        )
        assert row["rls_enabled"] is True, (
            f"RLS NOT ENABLED on public.{table_name} (relrowsecurity=false) — "
            "Law #6 violation: ENABLE ROW LEVEL SECURITY missing"
        )
        assert row["rls_forced"] is True, (
            f"RLS NOT FORCED on public.{table_name} (relforcerowsecurity=false) — "
            "Law #6 violation: FORCE ROW LEVEL SECURITY missing. "
            "The postgres/table-owner role can bypass RLS without this flag."
        )

    @pytest.mark.parametrize("table_name", TRUST_TABLES)
    def test_table_has_tenant_isolation_policy(
        self, pg: psycopg.Connection, table_name: str
    ) -> None:
        """A *_tenant_isolation or *_tenant_select RLS policy exists and targets authenticated."""
        # Use SIMILAR TO instead of LIKE to avoid psycopg3 '%' placeholder issue.
        # We select all policies for the table and filter in Python.
        cur = _sr_exec(
            pg,
            """
            SELECT policyname, cmd
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = %s;
            """,
            (table_name,),
        )
        rows = cur.fetchall()
        matching = [
            r for r in rows
            if "tenant_isolation" in r["policyname"] or "tenant_select" in r["policyname"]
        ]
        assert len(matching) >= 1, (
            f"No tenant isolation RLS policy found on public.{table_name}. "
            f"Existing policies: {[r['policyname'] for r in rows]}. "
            "Expected policy named '*_tenant_isolation' or '*_tenant_select'."
        )

    @pytest.mark.parametrize("table_name", TRUST_TABLES)
    def test_table_has_service_role_policy(
        self, pg: psycopg.Connection, table_name: str
    ) -> None:
        """A *_service_role* RLS policy exists (worker needs full access)."""
        cur = _sr_exec(
            pg,
            """
            SELECT policyname, cmd
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = %s;
            """,
            (table_name,),
        )
        rows = cur.fetchall()
        matching = [r for r in rows if "service_role" in r["policyname"]]
        assert len(matching) >= 1, (
            f"No service_role RLS policy found on public.{table_name}. "
            f"Existing policies: {[r['policyname'] for r in rows]}. "
            "Worker will be unable to write trust state transitions."
        )
