from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from aspire_orchestrator.services.provider_secret_registry import get_provider_secret_registry


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _adapters_dir() -> Path:
    return _backend_root() / "infrastructure" / "aws" / "rotation-lambdas" / "adapters"


def _terraform_path() -> Path:
    return _backend_root() / "infrastructure" / "aws" / "rotation" / "main.tf"


@lru_cache(maxsize=1)
def aws_rotation_adapter_names() -> set[str]:
    names = set()
    for path in _adapters_dir().glob("*_adapter.py"):
        if path.name == "base_adapter.py":
            continue
        names.add(path.stem.replace("_adapter", ""))
    return names


@lru_cache(maxsize=1)
def terraform_rotation_config() -> set[str]:
    text = _terraform_path().read_text(encoding="utf-8")
    marker = "rotation_config = {"
    start = text.find(marker)
    if start < 0:
        return set()
    brace_start = text.find("{", start)
    depth = 0
    end = brace_start
    for idx in range(brace_start, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break
    block = text[brace_start + 1:end]
    return set(re.findall(r"(?m)^\s*([a-z0-9_]+)\s*=\s*\{", block))


def build_rotation_inventory_report() -> dict[str, Any]:
    registry = list(get_provider_secret_registry())
    adapters = aws_rotation_adapter_names()
    terraform = terraform_rotation_config()

    counts = Counter(item.get("rotation_mode", "unknown") for item in registry)
    automated = [item for item in registry if item.get("rotation_mode") == "automated"]
    manual = [item for item in registry if item.get("rotation_mode") == "manual_alerted"]
    automated_ids = {item["provider"] for item in automated}
    automated_adapter_names = {
        item["provider"]: str(item.get("adapter_name") or "")
        for item in automated
    }
    missing_adapter_modules = sorted(
        provider
        for provider, adapter_name in automated_adapter_names.items()
        if adapter_name and adapter_name not in adapters
    )

    return {
        "counts": dict(counts),
        "automated_providers": sorted(automated_ids),
        "manual_alerted_providers": sorted(item["provider"] for item in manual),
        "aws_rotation_adapter_modules": sorted(adapters),
        "terraform_rotation_config": sorted(terraform),
        "missing_adapter_modules": missing_adapter_modules,
        "registry_automated_missing_from_terraform": sorted(automated_ids - terraform),
        "terraform_automated_missing_from_registry": sorted(terraform - automated_ids),
    }
