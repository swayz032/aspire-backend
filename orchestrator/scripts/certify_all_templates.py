#!/usr/bin/env python3
"""Certify All Templates

Batch certification script for CI/CD pipelines.
Reads template_registry.json and certifies all templates.

Usage:
    python scripts/certify_all_templates.py

Exit codes:
    0 - All templates certified
    1 - One or more templates failed certification
    2 - Script error

Outputs:
    certification_report.json - Detailed report with all results
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from certify_template import certify_template


def load_template_registry() -> dict[str, Any]:
    """Load template_registry.json from config directory.

    Returns:
        dict: Template registry data

    Raises:
        FileNotFoundError: If template_registry.json doesn't exist
    """
    # Find template_registry.json relative to this script
    script_dir = Path(__file__).parent
    registry_path = (
        script_dir.parent
        / "src"
        / "aspire_orchestrator"
        / "config"
        / "template_registry.json"
    )

    if not registry_path.exists():
        raise FileNotFoundError(f"Template registry not found at {registry_path}")

    with open(registry_path, "r") as f:
        return json.load(f)


async def certify_all_templates() -> dict[str, Any]:
    """Certify all templates in template_registry.json.

    Returns:
        dict: Certification report with:
            - total: int (total templates checked)
            - certified: int (templates that passed)
            - failed: int (templates that failed)
            - results: dict[str, dict] (per-template results)
            - failures: list[dict] (failed templates with reasons)
    """
    print("Loading template registry...", file=sys.stderr)
    registry = load_template_registry()

    templates = registry.get("templates", {})
    print(f"Found {len(templates)} templates to certify\n", file=sys.stderr)

    results = {}
    failures = []

    for template_key, template_spec in templates.items():
        template_uuid = template_spec.get("pandadoc_template_uuid")

        if not template_uuid:
            print(f"⚠️  Skipping {template_key}: No template UUID", file=sys.stderr)
            results[template_key] = {
                "certified": False,
                "reason": "No pandadoc_template_uuid configured",
            }
            failures.append({
                "template_key": template_key,
                "reason": "No pandadoc_template_uuid configured",
            })
            continue

        print(f"{'='*70}", file=sys.stderr)
        print(f"Certifying: {template_key}", file=sys.stderr)
        print(f"UUID: {template_uuid}", file=sys.stderr)
        print(f"{'='*70}\n", file=sys.stderr)

        try:
            result = await certify_template(template_uuid)
            results[template_key] = result

            if not result.get("certified", False):
                failures.append({
                    "template_key": template_key,
                    "template_uuid": template_uuid,
                    "fill_rate": result.get("fill_rate", 0.0),
                    "reason": result.get("reason", "Unknown failure"),
                })

        except Exception as e:
            print(f"❌ Error certifying {template_key}: {e}\n", file=sys.stderr)
            results[template_key] = {
                "certified": False,
                "reason": f"Error: {str(e)}",
            }
            failures.append({
                "template_key": template_key,
                "template_uuid": template_uuid,
                "reason": f"Error: {str(e)}",
            })

    # Generate summary
    total = len(templates)
    failed_count = len(failures)
    certified_count = total - failed_count

    print(f"\n{'='*70}", file=sys.stderr)
    print(f"CERTIFICATION SUMMARY", file=sys.stderr)
    print(f"{'='*70}\n", file=sys.stderr)
    print(f"Total templates: {total}", file=sys.stderr)
    print(f"✅ Certified: {certified_count}", file=sys.stderr)
    print(f"❌ Failed: {failed_count}", file=sys.stderr)

    if failures:
        print(f"\nFailed templates:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure['template_key']}: {failure['reason']}", file=sys.stderr)

    report = {
        "total": total,
        "certified": certified_count,
        "failed": failed_count,
        "results": results,
        "failures": failures,
    }

    return report


async def main() -> int:
    """Main entry point.

    Returns:
        int: Exit code (0 = all certified, 1 = failures, 2 = error)
    """
    try:
        report = await certify_all_templates()

        # Write report to file (for CI/CD artifact upload)
        report_path = Path("certification_report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        print(f"\n📄 Report written to: {report_path}", file=sys.stderr)

        # Also print to stdout for logging
        print(json.dumps(report, indent=2))

        # Return exit code based on failures
        if report["failed"] > 0:
            return 1
        else:
            return 0

    except Exception as e:
        print(f"❌ Fatal error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
