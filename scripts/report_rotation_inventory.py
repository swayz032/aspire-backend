#!/usr/bin/env python3
"""Report rotation coverage from the shared provider secret registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_SRC = REPO_ROOT / "orchestrator" / "src"
if str(ORCHESTRATOR_SRC) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_SRC))

from aspire_orchestrator.services.rotation_inventory import build_rotation_inventory_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    report = build_rotation_inventory_report()
    report["registry_path"] = str(REPO_ROOT / "config" / "provider_secret_registry.json")
    report["terraform_path"] = str(REPO_ROOT / "infrastructure" / "aws" / "rotation" / "main.tf")
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("Rotation Inventory")
    print(f"Registry: {report['registry_path']}")
    print(f"Terraform: {report['terraform_path']}")
    print(f"Counts: {report['counts']}")
    print(f"Automated: {', '.join(report['automated_providers']) or 'none'}")
    print(f"Manual alerted: {', '.join(report['manual_alerted_providers']) or 'none'}")
    print(f"Manual with adapters: {', '.join(report['manual_alerted_with_adapter_modules']) or 'none'}")
    print(f"Manual without adapters: {', '.join(report['manual_alerted_without_adapter_modules']) or 'none'}")
    print(f"Missing adapter modules: {', '.join(report['missing_adapter_modules']) or 'none'}")
    print(f"Registry automated missing from Terraform: {', '.join(report['registry_automated_missing_from_terraform']) or 'none'}")
    print(f"Terraform automated missing from registry: {', '.join(report['terraform_automated_missing_from_registry']) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
