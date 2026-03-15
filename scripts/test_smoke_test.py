"""Tests for the post-deploy smoke test script."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from scripts.smoke_test import ProbeResult, main, probe


class TestProbe:
    """Individual probe function tests."""

    def test_probe_success(self) -> None:
        with patch("scripts.smoke_test.httpx.get") as mock_get:
            mock_get.return_value = httpx.Response(200)
            result = probe("test-service", "http://localhost:8000/healthz")

        assert result.passed is True
        assert result.status_code == 200
        assert result.error is None

    def test_probe_server_error(self) -> None:
        with patch("scripts.smoke_test.httpx.get") as mock_get:
            mock_get.return_value = httpx.Response(500)
            result = probe("test-service", "http://localhost:8000/healthz")

        assert result.passed is False
        assert result.status_code == 500
        assert result.error == "HTTP 500"

    def test_probe_timeout(self) -> None:
        with patch("scripts.smoke_test.httpx.get", side_effect=httpx.TimeoutException("timed out")):
            result = probe("test-service", "http://localhost:8000/healthz")

        assert result.passed is False
        assert result.error == "timeout"

    def test_probe_connection_refused(self) -> None:
        with patch("scripts.smoke_test.httpx.get", side_effect=httpx.ConnectError("refused")):
            result = probe("test-service", "http://localhost:8000/healthz")

        assert result.passed is False
        assert "connection refused" in result.error  # type: ignore[operator]


class TestMain:
    """End-to-end main() tests."""

    def test_all_pass_returns_0(self) -> None:
        with patch("scripts.smoke_test.httpx.get") as mock_get:
            mock_get.return_value = httpx.Response(200)
            exit_code = main(["--backend", "http://b", "--desktop", "http://d", "--admin", "http://a"])

        assert exit_code == 0

    def test_any_failure_returns_1(self) -> None:
        def side_effect(url: str, **kwargs: object) -> httpx.Response:
            if "healthz" in url:
                return httpx.Response(500)
            return httpx.Response(200)

        with patch("scripts.smoke_test.httpx.get", side_effect=side_effect):
            exit_code = main(["--backend", "http://b", "--desktop", "http://d", "--admin", "http://a"])

        assert exit_code == 1
