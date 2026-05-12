"""Tests 1-11: serpapi_budget.py unit tests.

Contract tests covering:
  - select_account logic (A first, fallback to B, None when both exhausted)
  - try_increment in-memory path (DB mocked out)
  - get_api_key env var mapping (no real keys — uses monkeypatched env)
  - mark_account_exhausted forces count to cap
  - current_counts returns {A: n, B: m}
  - BudgetExhaustedError carries counts
  - Defensive list-wrap handling in try_increment (R1)
  - Deprecated cache.py stubs delegate correctly
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import aspire_orchestrator.services.adam.serpapi_budget as budget_module


@pytest.fixture(autouse=True)
def reset_budget():
    budget_module._reset_for_tests()
    yield
    budget_module._reset_for_tests()


def test_select_account_prefers_a_when_both_have_budget():
    result = budget_module.select_account()
    assert result == "A"


def test_select_account_falls_back_to_b_when_a_exhausted():
    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        budget_module._in_memory_counts[month] = {"A": budget_module.DEFAULT_CAP, "B": 0}
    result = budget_module.select_account()
    assert result == "B"


def test_select_account_returns_none_when_both_exhausted():
    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        budget_module._in_memory_counts[month] = {
            "A": budget_module.DEFAULT_CAP,
            "B": budget_module.DEFAULT_CAP,
        }
    result = budget_module.select_account()
    assert result is None


def test_try_increment_increments_count():
    result = budget_module.try_increment("A")
    assert result is True
    assert budget_module._get_count_for_account("A") == 1


def test_try_increment_returns_false_at_cap():
    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        budget_module._in_memory_counts[month] = {"A": budget_module.DEFAULT_CAP}
    result = budget_module.try_increment("A")
    assert result is False


def test_try_increment_handles_list_wrapped_rpc_result():
    mock_client = MagicMock()
    mock_client.rpc.return_value.execute.return_value.data = [5]
    with patch.object(budget_module, "_init_supabase", return_value=mock_client):
        budget_module._supabase_init_done = True
        budget_module._supabase_client = mock_client
        result = budget_module.try_increment("A")
    assert result is True


def test_try_increment_handles_none_rpc_result_as_at_cap():
    mock_client = MagicMock()
    mock_client.rpc.return_value.execute.return_value.data = None
    with patch.object(budget_module, "_init_supabase", return_value=mock_client):
        budget_module._supabase_init_done = True
        budget_module._supabase_client = mock_client
        result = budget_module.try_increment("A")
    assert result is False


def test_try_increment_handles_empty_list_rpc_result_as_at_cap():
    mock_client = MagicMock()
    mock_client.rpc.return_value.execute.return_value.data = []
    with patch.object(budget_module, "_init_supabase", return_value=mock_client):
        budget_module._supabase_init_done = True
        budget_module._supabase_client = mock_client
        result = budget_module.try_increment("A")
    assert result is False


def test_get_api_key_account_a_reads_serpapi_api_key(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "key-account-a")
    monkeypatch.delenv("ASPIRE_SERPAPI_API_KEY", raising=False)
    key = budget_module.get_api_key("A")
    assert key == "key-account-a"


def test_get_api_key_account_a_fallback_aspire_prefix(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("ASPIRE_SERPAPI_API_KEY", "key-account-a-aspire")
    key = budget_module.get_api_key("A")
    assert key == "key-account-a-aspire"


def test_get_api_key_account_b_reads_aspire_serpapi_2nd(monkeypatch):
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-account-b")
    key = budget_module.get_api_key("B")
    assert key == "key-account-b"


def test_get_api_key_missing_raises_key_error(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("ASPIRE_SERPAPI_API_KEY", raising=False)
    with pytest.raises(KeyError, match="SERPAPI_API_KEY not configured"):
        budget_module.get_api_key("A")


def test_get_api_key_account_b_missing_raises_key_error(monkeypatch):
    monkeypatch.delenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", raising=False)
    with pytest.raises(KeyError, match="ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY"):
        budget_module.get_api_key("B")


def test_get_api_key_unknown_account_raises_key_error():
    with pytest.raises(KeyError, match="Unknown SerpApi account_id"):
        budget_module.get_api_key("C")


def test_mark_account_exhausted_sets_count_to_cap():
    budget_module.mark_account_exhausted("A", reason="HTTP 429")
    assert budget_module._get_count_for_account("A") == budget_module.DEFAULT_CAP
    result = budget_module.select_account()
    assert result == "B"


def test_current_counts_returns_all_accounts():
    budget_module.try_increment("A")
    budget_module.try_increment("A")
    counts = budget_module.current_counts()
    assert "A" in counts
    assert "B" in counts
    assert counts["A"] == 2
    assert counts["B"] == 0


def test_budget_exhausted_error_carries_counts():
    counts = {"A": 240, "B": 240}
    exc = budget_module.BudgetExhaustedError(counts)
    assert exc.counts == counts
    assert "exhausted" in str(exc).lower()
    assert "240" in str(exc)


def test_try_increment_falls_back_to_in_memory_on_db_error():
    mock_client = MagicMock()
    mock_client.rpc.side_effect = Exception("DB unavailable")
    with patch.object(budget_module, "_init_supabase", return_value=mock_client):
        budget_module._supabase_init_done = True
        budget_module._supabase_client = mock_client
        result = budget_module.try_increment("A")
    assert result is True
    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        assert budget_module._in_memory_counts.get(month, {}).get("A", 0) == 1


# ---------------------------------------------------------------------------
# Test 12 (budget unit): mark_account_exhausted emits receipt (Fix 1 — Law #2)
# ---------------------------------------------------------------------------

def test_mark_account_exhausted_emits_receipt():
    """mark_account_exhausted must emit a receipt with the correct shape (Law #2)."""
    captured: list[list[dict]] = []

    def fake_store(receipts: list[dict]) -> None:
        captured.append(receipts)

    # Patch at the receipt_store module level — mark_account_exhausted does a
    # local import of store_receipts, so patching the source module is correct.
    with patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        side_effect=fake_store,
    ):
        budget_module.mark_account_exhausted("A", "http_429")

    assert len(captured) >= 1, "store_receipts was not called"
    receipt = captured[0][0]
    assert receipt["action_type"] == "external_api.budget.exhausted"
    assert receipt["outcome"] == "failed"
    assert receipt["reason_code"] == "SERPAPI_ACCOUNT_EXHAUSTED"
    assert receipt["redacted_outputs"]["account_id"] == "A"
    assert receipt["redacted_outputs"]["reason"] == "http_429"
    # Law #9: no API key material in receipt
    receipt_str = str(receipt)
    for secret_kw in ("serpapi_api_key", "SERPAPI_API_KEY", "api_key="):
        assert secret_kw not in receipt_str
