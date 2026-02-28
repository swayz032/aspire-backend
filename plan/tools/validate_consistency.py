#!/usr/bin/env python3
"""
validate_consistency.py - Validate cross-file consistency in the plan directory.

Checks:
  1. Phase durations are consistent across roadmap, phase files, and dependencies
  2. Gate counts match between gates/ directory and gates/README.md
  3. Phase frontmatter correctness (not_started phases should not claim satisfied gates)
  4. "Last Updated" date consistency (warn >7 days apart, error >30 days apart)
  5. Output: PASS/FAIL with itemized conflicts

Usage:
    python plan/tools/validate_consistency.py
"""

import io
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure stdout handles Unicode on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path.cwd()
PLAN_DIR = PROJECT_ROOT / "plan"
ROADMAP_PATH = PLAN_DIR / "Aspire-Production-Roadmap.md"
PHASES_DIR = PLAN_DIR / "phases"
DEPENDENCIES_PATH = PLAN_DIR / "00-dependencies.md"
GATES_DIR = PLAN_DIR / "gates"
GATES_README = GATES_DIR / "README.md"

# Date format commonly used in the plan files
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
DATE_FORMAT = "%Y-%m-%d"

# Week duration pattern: "Week X-Y" or "X-Y weeks" or "X weeks"
WEEK_RANGE_RE = re.compile(r"Week\s+(\d+)-(\d+)", re.IGNORECASE)
DURATION_WEEKS_RE = re.compile(r"(\d+)(?:-(\d+))?\s*weeks?", re.IGNORECASE)
DURATION_DAYS_RE = re.compile(r"(\d+)(?:-(\d+))?\s*days?", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_file_safe(path: Path) -> str:
    """Read a file, returning empty string if it does not exist or cannot be read."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def extract_frontmatter(content: str) -> dict[str, str]:
    """
    Extract YAML-like frontmatter from a markdown file (between --- delimiters).
    Returns a flat dict of key: value strings.
    """
    fm: dict[str, str] = {}
    lines = content.splitlines()

    in_frontmatter = False
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            else:
                break  # End of frontmatter
        if in_frontmatter and ":" in stripped:
            key, _, val = stripped.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def extract_dates(content: str) -> list[tuple[str, datetime]]:
    """
    Extract all dates from content along with their context line.
    Returns list of (context_snippet, datetime).
    """
    dates = []
    for line in content.splitlines():
        for match in DATE_RE.finditer(line):
            try:
                dt = datetime.strptime(match.group(1), DATE_FORMAT)
                context = line.strip()[:80]
                dates.append((context, dt))
            except ValueError:
                pass
    return dates


# ---------------------------------------------------------------------------
# Check 1: Phase Duration Consistency
# ---------------------------------------------------------------------------

def check_phase_durations(errors: list[str], warnings: list[str]) -> None:
    """
    Extract phase durations from:
      - Aspire-Production-Roadmap.md (Week X-Y patterns, duration mentions)
      - plan/phases/phase-*.md (frontmatter or body)
      - 00-dependencies.md (duration references)
    Flag conflicts between sources.
    """
    print("  [1] Checking phase duration consistency...")

    # Store durations by phase: { phase_id: { source: duration_str } }
    durations: dict[str, dict[str, str]] = {}

    # --- Source 1: Roadmap ---
    roadmap_content = read_file_safe(ROADMAP_PATH)
    if roadmap_content:
        # Look for phase summary table rows like:
        # | **0B** | 2-3 days | ...
        # | **1** | Week 3-10 | ...
        table_re = re.compile(
            r"\|\s*\*\*(\w+)\*\*\s*\|\s*([^|]+)\|",
            re.MULTILINE,
        )
        for match in table_re.finditer(roadmap_content):
            phase_id = match.group(1).strip()
            duration_cell = match.group(2).strip()
            if duration_cell and duration_cell != "Duration":
                durations.setdefault(phase_id, {})["roadmap"] = duration_cell
    else:
        warnings.append("  WARN [durations] Roadmap file not found or empty")

    # --- Source 2: Phase files ---
    if PHASES_DIR.exists():
        for phase_file in sorted(PHASES_DIR.glob("phase-*.md")):
            content = read_file_safe(phase_file)
            if not content:
                continue

            # Extract phase ID from filename (e.g., phase-0b-tower-setup.md -> 0b -> 0B)
            fname = phase_file.stem  # e.g., "phase-0b-tower-setup"
            parts = fname.split("-")
            if len(parts) >= 2:
                phase_id = parts[1].upper()
            else:
                continue

            # Try frontmatter first
            fm = extract_frontmatter(content)
            if "duration_estimate" in fm:
                durations.setdefault(phase_id, {})["phase_file_fm"] = fm["duration_estimate"]
            elif "duration" in fm:
                durations.setdefault(phase_id, {})["phase_file_fm"] = fm["duration"]

            # Also look for Duration lines in the body
            for line in content.splitlines():
                if re.match(r"\*\*Duration", line, re.IGNORECASE) or re.match(
                    r"Duration:", line, re.IGNORECASE
                ):
                    dur_match = DURATION_WEEKS_RE.search(line) or DURATION_DAYS_RE.search(line)
                    if dur_match:
                        durations.setdefault(phase_id, {})["phase_file_body"] = line.strip()[:60]
    else:
        warnings.append("  WARN [durations] phases/ directory not found")

    # --- Source 3: Dependencies file ---
    deps_content = read_file_safe(DEPENDENCIES_PATH)
    if deps_content:
        # Look for Duration: lines in context of PHASE sections
        current_phase = None
        for line in deps_content.splitlines():
            # Detect phase header: "PHASE 0B:" or "PHASE 1:"
            phase_header = re.match(r"PHASE\s+(\w+):", line, re.IGNORECASE)
            if phase_header:
                current_phase = phase_header.group(1).upper()
                continue

            if current_phase and "Duration:" in line:
                dur_str = line.split("Duration:", 1)[1].strip()
                if dur_str:
                    durations.setdefault(current_phase, {})["dependencies"] = dur_str[:60]
    else:
        warnings.append("  WARN [durations] 00-dependencies.md not found or empty")

    # --- Compare durations across sources ---
    if not durations:
        warnings.append("  WARN [durations] No phase durations extracted from any source")
        return

    conflict_count = 0
    for phase_id, sources in sorted(durations.items()):
        if len(sources) <= 1:
            continue

        # Normalize durations for comparison: extract numeric ranges
        normalized = {}
        for src, dur_str in sources.items():
            weeks = DURATION_WEEKS_RE.search(dur_str)
            days = DURATION_DAYS_RE.search(dur_str)
            week_range = WEEK_RANGE_RE.search(dur_str)
            if weeks:
                lo = int(weeks.group(1))
                hi = int(weeks.group(2)) if weeks.group(2) else lo
                normalized[src] = ("weeks", lo, hi)
            elif days:
                lo = int(days.group(1))
                hi = int(days.group(2)) if days.group(2) else lo
                normalized[src] = ("days", lo, hi)
            elif week_range:
                lo = int(week_range.group(1))
                hi = int(week_range.group(2))
                normalized[src] = ("week_range", lo, hi)
            else:
                normalized[src] = ("raw", 0, 0)

        # Check for conflicts: different numeric values between sources
        values = list(normalized.values())
        unique_values = set()
        for v in values:
            if v[0] != "raw":
                unique_values.add(v)

        if len(unique_values) > 1:
            conflict_count += 1
            detail = ", ".join(f"{src}='{dur}'" for src, dur in sources.items())
            warnings.append(
                f"  WARN [durations] Phase {phase_id}: possible duration conflict: {detail}"
            )

    if conflict_count == 0:
        print("      No duration conflicts found.")
    else:
        print(f"      {conflict_count} potential duration conflict(s) found.")


# ---------------------------------------------------------------------------
# Check 2: Gate Count Verification
# ---------------------------------------------------------------------------

def check_gate_counts(errors: list[str], warnings: list[str]) -> None:
    """
    Count actual .md files in plan/gates/ (excluding README.md) and
    compare with what gates/README.md claims.
    """
    print("  [2] Checking gate counts...")

    if not GATES_DIR.exists():
        errors.append("  FAIL [gates] gates/ directory does not exist")
        return

    # Count actual gate files (only numbered gates like gate-00-*, gate-01-*, etc.)
    gate_files = sorted([
        f for f in GATES_DIR.glob("gate-*.md")
        if f.name.lower() != "readme.md"
        and re.match(r"gate-\d{2}-", f.name)  # Only numbered gates (gate-00-, gate-01-, etc.)
    ])
    actual_count = len(gate_files)

    # Parse README to find claimed count
    readme_content = read_file_safe(GATES_README)
    claimed_count = None

    if readme_content:
        # Look for patterns like "All 11 gates defined" or "10/10 GATES"
        count_patterns = [
            re.compile(r"(\d+)\s*gates?\s*defined", re.IGNORECASE),
            re.compile(r"(\d+)/(\d+)\s*GATES", re.IGNORECASE),
            re.compile(r"Overall.*?(\d+)/(\d+)", re.IGNORECASE),
        ]
        for pattern in count_patterns:
            match = pattern.search(readme_content)
            if match:
                # Use the first number if only one group, or second if two groups
                if match.lastindex and match.lastindex >= 2:
                    claimed_count = int(match.group(2))
                else:
                    claimed_count = int(match.group(1))
                break

    if claimed_count is None:
        warnings.append("  WARN [gates] Could not extract gate count from README.md")
        print(f"      Actual gate files: {actual_count} (README count not determined)")
    elif actual_count != claimed_count:
        errors.append(
            f"  FAIL [gates] Gate count mismatch: {actual_count} files vs "
            f"{claimed_count} claimed in README.md"
        )
        print(f"      MISMATCH: {actual_count} files vs {claimed_count} in README")
        # List the actual files for clarity
        for gf in gate_files:
            print(f"        - {gf.name}")
    else:
        print(f"      Gate count matches: {actual_count} files, {claimed_count} claimed")


# ---------------------------------------------------------------------------
# Check 3: Phase Frontmatter Correctness
# ---------------------------------------------------------------------------

def check_phase_frontmatter(errors: list[str], warnings: list[str]) -> None:
    """
    For phases with status "not_started", verify gates_satisfied is empty
    (or renamed to gates_targeted). Flag any not_started phase that claims
    gates are satisfied.
    """
    print("  [3] Checking phase frontmatter correctness...")

    if not PHASES_DIR.exists():
        warnings.append("  WARN [frontmatter] phases/ directory not found")
        return

    checked = 0
    for phase_file in sorted(PHASES_DIR.glob("phase-*.md")):
        content = read_file_safe(phase_file)
        if not content:
            continue

        fm = extract_frontmatter(content)
        if not fm:
            continue

        checked += 1
        status = fm.get("status", "").lower()
        gates_satisfied = fm.get("gates_satisfied", "").strip()
        fname = phase_file.name

        if status == "not_started":
            # gates_satisfied should be empty or "[]"
            if gates_satisfied and gates_satisfied not in ("[]", "null", "none", ""):
                errors.append(
                    f"  FAIL [frontmatter] {fname}: status is 'not_started' but "
                    f"gates_satisfied is '{gates_satisfied}' (should be empty)"
                )
            else:
                # Also check gates_targeted existence (renamed field)
                gates_targeted = fm.get("gates_targeted", "")
                # This is fine -- gates_targeted is the correct field for not_started

    if checked == 0:
        warnings.append("  WARN [frontmatter] No phase files with frontmatter found")
    else:
        print(f"      Checked {checked} phase files.")


# ---------------------------------------------------------------------------
# Check 4: Last Updated Date Consistency
# ---------------------------------------------------------------------------

def check_date_consistency(errors: list[str], warnings: list[str]) -> None:
    """
    Extract dates from all plan/ .md files. Warn if any are >7 days apart
    from each other. Error if any are >30 days apart.
    Only considers "Last Updated" or similar header dates, not all dates.
    """
    print("  [4] Checking 'Last Updated' date consistency...")

    # Collect "Last Updated" dates from plan/ .md files (non-recursive to avoid
    # ecosystem scan noise, then add phase/gate files)
    target_files = []

    # Top-level plan files
    target_files.extend(sorted(PLAN_DIR.glob("*.md")))

    # Phase files
    if PHASES_DIR.exists():
        target_files.extend(sorted(PHASES_DIR.glob("phase-*.md")))

    # Gate files
    if GATES_DIR.exists():
        target_files.extend(sorted(GATES_DIR.glob("*.md")))

    # Registry directory
    registries_dir = PLAN_DIR / "registries"
    if registries_dir.exists():
        for reg_file in sorted(registries_dir.glob("*.yaml")):
            target_files.append(reg_file)

    # Reference directory
    reference_dir = PLAN_DIR / "reference"
    if reference_dir.exists():
        target_files.extend(sorted(reference_dir.glob("*.md")))

    last_updated_re = re.compile(
        r"(?:Last\s+Updated|last_updated|Updated)\s*[:=]\s*(\d{4}-\d{2}-\d{2})",
        re.IGNORECASE,
    )

    file_dates: list[tuple[str, datetime]] = []

    for fpath in target_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        for line in content.splitlines()[:30]:  # Only check first 30 lines
            match = last_updated_re.search(line)
            if match:
                try:
                    dt = datetime.strptime(match.group(1), DATE_FORMAT)
                    rel_path = str(fpath.relative_to(PROJECT_ROOT))
                    file_dates.append((rel_path, dt))
                except ValueError:
                    pass
                break  # Only take first match per file

    if len(file_dates) < 2:
        warnings.append(
            f"  WARN [dates] Only {len(file_dates)} file(s) with 'Last Updated' dates found "
            f"(need 2+ to compare)"
        )
        return

    # Find min and max dates
    all_dates = [d for _, d in file_dates]
    min_date = min(all_dates)
    max_date = max(all_dates)
    spread = (max_date - min_date).days

    print(f"      Date range: {min_date.strftime(DATE_FORMAT)} to {max_date.strftime(DATE_FORMAT)} ({spread} days spread)")
    print(f"      Files with dates: {len(file_dates)}")

    if spread > 30:
        # Find the specific outliers
        oldest_file = min(file_dates, key=lambda x: x[1])
        newest_file = max(file_dates, key=lambda x: x[1])
        errors.append(
            f"  FAIL [dates] 'Last Updated' dates span {spread} days (>30 day threshold):\n"
            f"        Oldest: {oldest_file[0]} ({oldest_file[1].strftime(DATE_FORMAT)})\n"
            f"        Newest: {newest_file[0]} ({newest_file[1].strftime(DATE_FORMAT)})"
        )
    elif spread > 7:
        oldest_file = min(file_dates, key=lambda x: x[1])
        newest_file = max(file_dates, key=lambda x: x[1])
        warnings.append(
            f"  WARN [dates] 'Last Updated' dates span {spread} days (>7 day threshold):\n"
            f"        Oldest: {oldest_file[0]} ({oldest_file[1].strftime(DATE_FORMAT)})\n"
            f"        Newest: {newest_file[0]} ({newest_file[1].strftime(DATE_FORMAT)})"
        )
    else:
        print(f"      All dates within 7-day window. OK.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not PLAN_DIR.exists():
        print(f"ERROR: plan/ directory not found at {PLAN_DIR}")
        return 1

    errors: list[str] = []
    warnings: list[str] = []

    print("=" * 72)
    print("  VALIDATE CONSISTENCY")
    print("=" * 72)
    print()

    # Run all checks
    check_phase_durations(errors, warnings)
    print()
    check_gate_counts(errors, warnings)
    print()
    check_phase_frontmatter(errors, warnings)
    print()
    check_date_consistency(errors, warnings)
    print()

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print("-" * 72)
    print(f"  Errors:   {len(errors)}")
    print(f"  Warnings: {len(warnings)}")
    print()

    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(w)
        print()

    if errors:
        print("ERRORS:")
        for err in errors:
            print(err)
        print()

    overall_pass = len(errors) == 0
    print("=" * 72)
    print(f"  RESULT: {'PASS' if overall_pass else 'FAIL'}")
    print("=" * 72)

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
