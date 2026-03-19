from aspire_orchestrator.launch import resolve_port


def test_resolve_port_prefers_platform_port(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "8123")
    assert resolve_port() == 8123


def test_resolve_port_falls_back_for_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "not-a-port")
    assert resolve_port() == 8000


def test_resolve_port_falls_back_for_out_of_range(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "70000")
    assert resolve_port() == 8000
