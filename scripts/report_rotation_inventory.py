#!/usr/bin/env python3
"""Report rotation coverage from the shared provider secret registry."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "config" / "provider_secret_registry.json"
ADAPTERS_DIR = REPO_ROOT / "infrastructure" / "aws" / "rotation-lambdas" / "adapters"
TERRAFORM_PATH = REPO_ROOT / "infrastructure" / "aws" / "rotation" / "main.tf"


def load_registry() -> list[dict[str, Any]]:
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("provider_secret_registry.json must be a list")
    return data


def aws_adapter_names() -> set[str]:
    names = set()
    for path in ADAPTERS_DIR.glob("*_adapter.py"):
        if path.name == "base_adapter.py":
            continue
        names.add(path.stem.replace("_adapter", ""))
    return names


def terraform_automated_providers() -> set[str]:
    text = TERRAFORM_PATH.read_text(encoding="utf-8")
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


def build_report() -> dict[str, Any]:
    registry = load_registry()
    adapters = aws_adapter_names()
    terraform = terraform_automated_providers()

    automated = [item for item in registry if item.get("rotation_mode") == "automated"]
    manual = [item for item in registry if item.get("rotation_mode") == "manual_alerted"]
    counts = Counter(item.get("rotation_mode", "unknown") for item in registry)

    automated_ids = {item["provider"] for item in automated}
    automated_adapter_names = {
        item["provider"]: item.get("adapter_name", "")
        for item in automated
    }
    missing_adapter_modules = sorted(
        provider
        for provider, adapter_name in automated_adapter_names.items()
        if adapter_name and adapter_name not in adapters
    )
    terraform_missing = sorted(automated_ids - terraform)
    terraform_untracked = sorted(terraform - automated_ids)

    return {
        "registry_path": str(REGISTRY_PATH),
        "terraform_path": str(TERRAFORM_PATH),
        "counts": dict(counts),
        "automated_providers": sorted(automated_ids),
        "manual_alerted_providers": sorted(item["provider"] for item in manual),
        "aws_rotation_adapter_modules": sorted(adapters),
        "terraform_rotation_config": sorted(terraform),
        "missing_adapter_modules": missing_adapter_modules,
        "terraform_missing_from_registry_automated": terraform_untracked,
        "registry_automated_missing_from_terraform": terraform_missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("Rotation Inventory")
    print(f"Registry: {report['registry_path']}")
    print(f"Terraform: {report['terraform_path']}")
    print(f"Counts: {report['counts']}")
    print(f"Automated: {', '.join(report['automated_providers']) or 'none'}")
    print(f"Manual alerted: {', '.join(report['manual_alerted_providers']) or 'none'}")
    print(f"Missing adapter modules: {', '.join(report['missing_adapter_modules']) or 'none'}")
    print(f"Registry automated missing from Terraform: {', '.join(report['registry_automated_missing_from_terraform']) or 'none'}")
    print(f"Terraform automated missing from registry: {', '.join(report['terraform_missing_from_registry_automated']) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
