#!/usr/bin/env python3

"""
Aspire Session Briefing Generator

Reads skills files and the latest critic report, prints a compact briefing
to stdout. Called by the SessionStart hook so that Claude Code receives
learned skill rules and pending proposals in-context automatically.

Design constraints:
- Stdlib only (pathlib, json, re, argparse). No dependencies.
- Sub-100ms: reads ~4 small markdown files + 1 JSON.
- Silent failures: missing/malformed files are skipped. Always exits 0.
- Compact: output stays under ~2500 chars total.
- Skips placeholders like "- (append entries here)".
"""

import argparse
import json
import pathlib
import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLACEHOLDER_PATTERN = re.compile(
    r"^\(.*(?:append|add|placeholder|todo|tbd|fixme).*\)$",
    re.IGNORECASE,
)

# Skill files in display order: (relative_path, display_name, description)
SKILL_FILES = [
    ("global/SAFETY.md", "SAFETY", "hard constraints"),
    ("global/STYLE.md", "STYLE", "conventions"),
    ("aspire/DEBUGGING.md", "DEBUGGING", "workflow"),
    ("aspire/RECEIPTS.md", "RECEIPTS", "invariants"),
]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_rules(path: pathlib.Path) -> list[str]:
    """Extract bullet-point rules from a skill markdown file.

    Captures all lines starting with "- " that aren't placeholders,
    from any section (not just Changelog).
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    rules: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        content = stripped[2:].strip()
        if len(content) < 4:
            continue
        if PLACEHOLDER_PATTERN.match(content):
            continue
        rules.append(content)
    return rules


def find_latest_bundle(proposed_root: pathlib.Path) -> pathlib.Path | None:
    """Find the most recent reflect-* bundle that contains a critic report."""
    if not proposed_root.is_dir():
        return None
    bundles = sorted(proposed_root.glob("reflect-*"), reverse=True)
    for b in bundles:
        if b.is_dir() and (b / "critic-report.json").exists():
            return b
    return None


def load_critic_report(bundle_dir: pathlib.Path) -> dict | None:
    """Load and parse critic-report.json from a bundle directory."""
    report_path = bundle_dir / "critic-report.json"
    if not report_path.is_file():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def format_skills_section(skills_root: pathlib.Path) -> str:
    """Format the active skills rules section."""
    sections: list[str] = []

    for rel_path, name, desc in SKILL_FILES:
        full_path = skills_root / rel_path
        rules = extract_rules(full_path)
        if not rules:
            continue
        header = f"{name} ({desc}):"
        bullets = "\n".join(f"- {r}" for r in rules)
        sections.append(f"{header}\n{bullets}")

    if not sections:
        return ""

    return (
        "=== LEARNED SKILL RULES (ACTIVE — FOLLOW THESE) ===\n\n"
        + "\n\n".join(sections)
    )


def format_proposals_section(proposed_root: pathlib.Path) -> str:
    """Format the pending proposals section from the latest critic report."""
    bundle = find_latest_bundle(proposed_root)
    if bundle is None:
        return ""

    report = load_critic_report(bundle)
    if report is None:
        return ""

    reviews = report.get("reviews", [])
    if not reviews:
        return ""

    # Group by verdict
    by_verdict: dict[str, list[dict]] = {}
    for review in reviews:
        verdict = review.get("verdict", "unknown")
        by_verdict.setdefault(verdict, []).append(review)

    bundle_name = bundle.name
    lines: list[str] = [
        "=== PENDING PROPOSALS (REVIEW WITH TONIO) ===",
        f"Bundle: proposed/{bundle_name}/",
    ]

    for verdict, items in by_verdict.items():
        target_files = [
            pathlib.Path(r.get("targetFile", "?")).name for r in items
        ]
        total_score = items[0].get("scores", {}).get("total", "?")
        reasoning = items[0].get("reasoning", "")
        # Truncate reasoning to first sentence for compactness
        short_reason = reasoning.split(".")[0] + "." if reasoning else ""
        lines.append(
            f"  {verdict} ({len(items)}): {', '.join(target_files)}"
            f" — \"{short_reason}\" ({total_score}/20)"
        )

    return "\n".join(lines)


def format_reminders() -> str:
    """Format the session reminders section."""
    return (
        "=== SESSION REMINDERS ===\n"
        "- Write corrections to .claude/session-notes.md during this session\n"
        "- Format: \"Correction: ...\", \"Pattern: ...\", \"Never: ...\", \"Approval: ...\"\n"
        "- These get processed into skill proposals when the session ends"
    )


def generate_briefing(skills_root: pathlib.Path, proposed_root: pathlib.Path) -> str:
    """Generate the full briefing string."""
    parts: list[str] = []

    skills_section = format_skills_section(skills_root)
    if skills_section:
        parts.append(skills_section)

    proposals_section = format_proposals_section(proposed_root)
    if proposals_section:
        parts.append(proposals_section)

    parts.append(format_reminders())

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Generate session briefing from skills and critic reports.",
    )
    ap.add_argument(
        "--skills-root",
        default="skills",
        help="Path to skills/ directory (default: skills/)",
    )
    ap.add_argument(
        "--proposed",
        default="proposed",
        help="Path to proposed/ directory (default: proposed/)",
    )
    args = ap.parse_args()

    skills_root = pathlib.Path(args.skills_root)
    proposed_root = pathlib.Path(args.proposed)

    output = generate_briefing(skills_root, proposed_root)
    if output:
        print(output)


if __name__ == "__main__":
    main()
