from __future__ import annotations

import py_compile
from pathlib import Path

from aspire_orchestrator.services.rotation_inventory import build_rotation_inventory_report


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_rotation_lambda_adapter_sources_compile() -> None:
    root = _backend_root()
    paths = [
        root / "infrastructure" / "aws" / "rotation-lambdas" / "adapters" / "deepgram_adapter.py",
        root / "infrastructure" / "aws" / "rotation-lambdas" / "adapters" / "elevenlabs_adapter.py",
        root / "infrastructure" / "aws" / "rotation-lambdas" / "adapters" / "__init__.py",
        root / "infrastructure" / "aws" / "rotation-lambdas" / "handlers" / "rotation_handler.py",
    ]
    for path in paths:
        py_compile.compile(str(path), doraise=True)


def test_rotation_inventory_reports_adapter_ready_manual_providers() -> None:
    report = build_rotation_inventory_report()

    assert "deepgram" in report["manual_alerted_with_adapter_modules"]
    assert "elevenlabs" in report["manual_alerted_with_adapter_modules"]
    assert "deepgram" not in report["manual_alerted_without_adapter_modules"]
    assert "elevenlabs" not in report["manual_alerted_without_adapter_modules"]
