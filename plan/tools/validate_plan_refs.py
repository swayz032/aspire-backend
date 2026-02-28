#!/usr/bin/env python3
"""
validate_plan_refs.py - Validate all plan references are correct.

Walks all .md files in plan/ directory recursively and checks:
  1. No "Trust Spine Package" string appears anywhere
  2. No "PHASE_2_SUBSTRATE_VALIDATION" appears anywhere
  3. No "PHASE_3_INTELLIGENCE_INTEGRATION" appears anywhere
  4. All markdown links [text](path) resolve to existing files
  5. No "109 migrations" string appears
  6. Output: PASS/FAIL with exact file:line for each error

Usage:
    python plan/tools/validate_plan_refs.py
"""

import io
import re
import sys
from pathlib import Path

# Ensure stdout handles Unicode on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Project root is assumed to be the current working directory
PROJECT_ROOT = Path.cwd()
PLAN_DIR = PROJECT_ROOT / "plan"
ECOSYSTEM_ROOT = PLAN_DIR / "temp_ecosystem_scan" / "aspire_ecosystem_v12.7_2026-02-03"

# Forbidden strings: each entry is (pattern, human-readable label)
FORBIDDEN_STRINGS = [
    ("Trust Spine Package", "Trust Spine Package"),
    ("PHASE_2_SUBSTRATE_VALIDATION", "PHASE_2_SUBSTRATE_VALIDATION"),
    ("PHASE_3_INTELLIGENCE_INTEGRATION", "PHASE_3_INTELLIGENCE_INTEGRATION"),
    ("PHASE_4_PROVIDER_INTEGRATIONS", "PHASE_4_PROVIDER_INTEGRATIONS"),
    ("PHASE_6_MOBILE_INTEGRATION", "PHASE_6_MOBILE_INTEGRATION"),
    ("PHASE_7_PRODUCTION_OPERATIONS", "PHASE_7_PRODUCTION_OPERATIONS"),
    ("PHASE_8_SCALE", "PHASE_8_SCALE"),
    ("ADR-0001-suite-office-identity.md", "ADR-0001-suite-office-identity.md"),
    ("ADR-0008-release-gates.md", "ADR-0008-release-gates.md"),
    ("A2A_INBOX_V6/go/", "A2A_INBOX_V6/go/"),
    ("109 migrations", "109 migrations"),
]

# Files excluded from forbidden-string checks (they document stale refs intentionally)
FORBIDDEN_STRING_EXCLUDE_FILES = {
    "CANONICAL_PATHS.md",       # Stale references table (documents what NOT to use)
    "SYNC-AUDIT-REPORT.md",    # Historical audit record (append-only)
}

# Directories excluded from all checks (external/vendored content)
EXCLUDED_DIRS = {
    "temp_ecosystem_scan",     # Ecosystem zip extract (read-only reference)
}

# Regex to find markdown links: [text](path)
# Excludes URLs (http/https), anchors-only (#), and mailto: links
MARKDOWN_LINK_RE = re.compile(
    r'\[(?P<text>[^\]]*)\]\((?P<path>[^)]+)\)'
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_external_link(path_str: str) -> bool:
    """Return True if the link is external (URL, anchor-only, or mailto)."""
    stripped = path_str.strip()
    if stripped.startswith(("http://", "https://", "mailto:", "#")):
        return True
    return False


def strip_anchor(path_str: str) -> str:
    """Remove a trailing #anchor from a path string."""
    idx = path_str.find("#")
    if idx != -1:
        return path_str[:idx]
    return path_str


def resolve_link(md_file: Path, link_path: str) -> Path:
    """
    Resolve a markdown link path relative to the file that contains it.
    Handles both plan-relative and file-relative paths.
    """
    link_clean = strip_anchor(link_path.strip())
    if not link_clean:
        # Anchor-only link after stripping -- treat as valid
        return md_file

    candidate = md_file.parent / link_clean
    return candidate


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def check_forbidden_strings(md_file: Path, lines: list[str], errors: list[str]) -> None:
    """Check for forbidden strings in the file content."""
    for line_num, line in enumerate(lines, start=1):
        for pattern, label in FORBIDDEN_STRINGS:
            if pattern in line:
                errors.append(
                    f"  FAIL {md_file.relative_to(PROJECT_ROOT)}:{line_num} "
                    f"-- forbidden string '{label}' found"
                )


def check_markdown_links(md_file: Path, lines: list[str], errors: list[str]) -> None:
    """Check that all markdown links point to existing files."""
    for line_num, line in enumerate(lines, start=1):
        for match in MARKDOWN_LINK_RE.finditer(line):
            link_path = match.group("path")

            # Skip external links
            if is_external_link(link_path):
                continue

            # Strip anchor
            clean_path = strip_anchor(link_path.strip())
            if not clean_path:
                continue

            # Resolve the path relative to the markdown file's directory
            resolved = resolve_link(md_file, clean_path)

            # Also try resolving relative to project root (some links use plan/...)
            resolved_from_root = PROJECT_ROOT / clean_path

            if not resolved.exists() and not resolved_from_root.exists():
                errors.append(
                    f"  FAIL {md_file.relative_to(PROJECT_ROOT)}:{line_num} "
                    f"-- broken link [{match.group('text')}]({link_path}) "
                    f"-> {resolved.relative_to(PROJECT_ROOT)} does not exist"
                )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not PLAN_DIR.exists():
        print(f"ERROR: plan/ directory not found at {PLAN_DIR}")
        return 1

    errors: list[str] = []
    files_checked = 0

    # Walk all .md files in plan/ recursively
    for md_file in sorted(PLAN_DIR.rglob("*.md")):
        # Skip excluded directories
        rel_parts = md_file.relative_to(PLAN_DIR).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue

        files_checked += 1
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors.append(f"  FAIL {md_file.relative_to(PROJECT_ROOT)} -- cannot read: {e}")
            continue

        lines = content.splitlines()

        # Run forbidden string checks (skip excluded files)
        if md_file.name not in FORBIDDEN_STRING_EXCLUDE_FILES:
            check_forbidden_strings(md_file, lines, errors)
        check_markdown_links(md_file, lines, errors)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print("=" * 72)
    print("  VALIDATE PLAN REFS")
    print("=" * 72)
    print(f"  Files scanned: {files_checked}")
    print(f"  Errors found:  {len(errors)}")
    print()

    if errors:
        print("ERRORS:")
        print()
        for err in errors:
            print(err)
        print()
        print("=" * 72)
        print("  RESULT: FAIL")
        print("=" * 72)
        return 1
    else:
        print("  All checks passed.")
        print()
        print("=" * 72)
        print("  RESULT: PASS")
        print("=" * 72)
        return 0


if __name__ == "__main__":
    sys.exit(main())
