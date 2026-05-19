"""Symbol detector unit tests — Wave 9k.

Verifies the YOLO default-model contract introduced in Wave 9k:
  - Default model is yolo11n.pt (nano, ~3x faster CPU inference).
  - ASPIRE_DREW_YOLO_MODEL env override still works.
  - Confidence floor remains 0.70 (Wave 3 plan §1).

These are deterministic, no YOLO weights required.
"""

from __future__ import annotations

import importlib
import os

import pytest


class TestSymbolDetectorDefaults:
    def test_default_model_is_yolo11n(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wave 9k: default weights must be yolo11n.pt when no env override is set."""
        monkeypatch.delenv("ASPIRE_DREW_YOLO_MODEL", raising=False)
        # Re-import to pick up env state at module load time.
        from aspire_orchestrator.services.blueprint import symbol_detector as _sd
        importlib.reload(_sd)
        assert _sd._DEFAULT_MODEL == "yolo11n.pt", (
            "Wave 9k contract: default YOLO model must be yolo11n.pt (nano) "
            "for ~3x faster CPU inference. yolo11m.pt remains available via "
            "ASPIRE_DREW_YOLO_MODEL env override."
        )

    def test_env_override_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASPIRE_DREW_YOLO_MODEL", "yolo11m.pt")
        from aspire_orchestrator.services.blueprint import symbol_detector as _sd
        importlib.reload(_sd)
        assert _sd._DEFAULT_MODEL == "yolo11m.pt"

    def test_confidence_floor_is_seventy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ASPIRE_DREW_YOLO_MODEL", raising=False)
        from aspire_orchestrator.services.blueprint import symbol_detector as _sd
        importlib.reload(_sd)
        assert _sd._CONFIDENCE_FLOOR == 0.70


@pytest.fixture(autouse=True)
def _restore_module_state():
    """Restore symbol_detector to env-driven default after each test."""
    yield
    os.environ.pop("ASPIRE_DREW_YOLO_MODEL", None)
    from aspire_orchestrator.services.blueprint import symbol_detector as _sd
    importlib.reload(_sd)
