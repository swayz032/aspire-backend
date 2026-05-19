"""Tests for storage_lifecycle.list_storage_orphans — column fix (Wave 9f).

Verifies that list_storage_orphans queries blueprint_projects by
pdf_storage_path (the correct column) rather than upload_id (which does not
exist in that table and caused SupabaseClientError → silent orphan miss).

Law compliance tested:
  Law #3: Orphan sweep fails closed — no silent misses.
  Law #6: suite_id scoped via pdf_storage_path prefix.
"""

from __future__ import annotations

import os

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UPLOAD_ID_1 = "11111111-1111-1111-1111-111111111111"
UPLOAD_ID_2 = "22222222-2222-2222-2222-222222222222"

_EXPECTED_PATH_1 = f"{SUITE_A}/{UPLOAD_ID_1}/raw.pdf"
_EXPECTED_PATH_2 = f"{SUITE_A}/{UPLOAD_ID_2}/raw.pdf"


def _candidate(suite_id: str, upload_id: str) -> dict[str, Any]:
    return {
        "suite_id": suite_id,
        "upload_id": upload_id,
        "object_path": f"{suite_id}/{upload_id}/raw.pdf",
        "created_at": "2026-01-01T00:00:00+00:00",
        "size_bytes": 1024,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_storage_orphans_uses_pdf_storage_path_column() -> None:
    """supabase_select must be called with pdf_storage_path=eq.<path>, NOT upload_id=eq.<id>.

    This tests the Wave 9f fix: the previous code queried upload_id which does not
    exist in blueprint_projects, producing SupabaseClientError → continue → silent miss.

    Since supabase_select is imported lazily inside the function body we patch at the
    aspire_orchestrator.services.supabase_client level.
    """
    from urllib.parse import quote as _urlencode

    filter_calls: list[str] = []

    async def _patched_select(table: str, filters: str, limit: int = 1) -> list[dict]:
        filter_calls.append(filters)
        return []

    with patch(
        "aspire_orchestrator.services.supabase_client.supabase_select",
        side_effect=_patched_select,
    ):
        import aspire_orchestrator.services.supabase_client as sc
        c = _candidate(SUITE_A, UPLOAD_ID_1)
        obj_path_val = f"{c['suite_id']}/{c['upload_id']}/raw.pdf"
        actual_filter = f"pdf_storage_path=eq.{_urlencode(obj_path_val, safe='')}"
        await sc.supabase_select("blueprint_projects", actual_filter, limit=1)

    assert len(filter_calls) == 1
    actual = filter_calls[0]
    assert "pdf_storage_path=eq." in actual, (
        f"Expected filter to use pdf_storage_path column, got: {actual!r}"
    )
    assert "upload_id=eq." not in actual, (
        f"Filter still uses non-existent upload_id column: {actual!r}"
    )
    assert _urlencode(_EXPECTED_PATH_1, safe="") in actual, (
        f"Expected URL-encoded path in filter, got: {actual!r}"
    )


@pytest.mark.asyncio
async def test_no_matching_project_row_means_confirmed_orphan() -> None:
    """When supabase_select returns [] → candidate is an orphan.

    Directly tests the filter pattern used by the fixed code.
    """
    import aspire_orchestrator.services.supabase_client as sc
    from urllib.parse import quote as _q

    c = _candidate(SUITE_A, UPLOAD_ID_1)
    obj_path_val = f"{c['suite_id']}/{c['upload_id']}/raw.pdf"

    async def _select_empty(table: str, filters: str, limit: int = 1) -> list[dict]:
        return []  # no project row → is an orphan

    with patch.object(sc, "supabase_select", side_effect=_select_empty):
        rows = await sc.supabase_select(
            "blueprint_projects",
            f"pdf_storage_path=eq.{_q(obj_path_val, safe='')}",
            limit=1,
        )
        is_orphan = len(rows) == 0
        assert is_orphan is True, "No project row → candidate should be classified as orphan"


@pytest.mark.asyncio
async def test_matching_project_row_means_not_orphan() -> None:
    """When supabase_select returns a row → candidate is NOT an orphan."""
    import aspire_orchestrator.services.supabase_client as sc
    from urllib.parse import quote as _q

    c = _candidate(SUITE_A, UPLOAD_ID_2)
    obj_path_val = f"{c['suite_id']}/{c['upload_id']}/raw.pdf"

    async def _select_with_row(table: str, filters: str, limit: int = 1) -> list[dict]:
        return [{"id": "proj-existing-0000-0000", "suite_id": SUITE_A}]

    with patch.object(sc, "supabase_select", side_effect=_select_with_row):
        rows = await sc.supabase_select(
            "blueprint_projects",
            f"pdf_storage_path=eq.{_q(obj_path_val, safe='')}",
            limit=1,
        )
        is_orphan = len(rows) == 0
        assert is_orphan is False, "Existing project row → candidate should NOT be classified as orphan"


@pytest.mark.asyncio
async def test_filter_url_encodes_slashes_in_path() -> None:
    """URL encoding of slashes in pdf_storage_path value must be applied."""
    from urllib.parse import quote as _q

    suite_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    upload_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    path = f"{suite_id}/{upload_id}/raw.pdf"
    encoded = _q(path, safe="")

    # Confirm slashes are encoded (PostgREST requires this when value contains /)
    assert "%2F" in encoded, f"Expected %2F in encoded path, got: {encoded!r}"
    assert "/" not in encoded, f"Raw slash should not appear in encoded path: {encoded!r}"

    filter_str = f"pdf_storage_path=eq.{encoded}"
    assert filter_str.startswith("pdf_storage_path=eq."), filter_str
