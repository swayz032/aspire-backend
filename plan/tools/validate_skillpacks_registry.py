#!/usr/bin/env python3
"""
validate_skillpacks_registry.py - Validate the skill pack registry.

Checks:
  1. Parses plan/registries/skill-pack-registry.yaml
  2. For each skill pack, verifies manifest_path points to a real file
  3. Verifies id/name consistency (no hyphen/underscore mismatches)
  4. Cross-references with ecosystem skillpacks/ directory if it exists
  5. Checks for certification_schema field reference
  6. Output: PASS/FAIL per skill pack, summary at end

Usage:
    python plan/tools/validate_skillpacks_registry.py
    python plan/tools/validate_skillpacks_registry.py --strict
        (--strict fails on missing manifests even for designed-status packs)
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

PROJECT_ROOT = Path.cwd()
REGISTRY_PATH = PROJECT_ROOT / "plan" / "registries" / "skill-pack-registry.yaml"
ECOSYSTEM_SKILLPACKS = (
    PROJECT_ROOT / "plan" / "temp_ecosystem_scan"
    / "aspire_ecosystem_v12.7_2026-02-03" / "skillpacks"
)

# Sections in the YAML that contain skill pack entries
SKILL_PACK_SECTIONS = [
    "channel_skill_packs",
    "finance_skill_packs",
    "legal_skill_packs",
    "internal_admin_skill_packs",
    "internal_skill_packs",
]


# ---------------------------------------------------------------------------
# YAML Loading
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    """
    Load a YAML file. Tries PyYAML first; falls back to basic manual parsing.
    """
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except ImportError:
        print("  WARNING: PyYAML not installed. Falling back to basic parsing.")
        print("           Install with: pip install pyyaml")
        print()
        return _basic_yaml_parse(path)


def _basic_yaml_parse(path: Path) -> dict:
    """
    Very basic YAML-like parser that extracts skill pack entries.
    Only handles the specific structure of skill-pack-registry.yaml.
    Returns a dict with section keys mapping to dicts of skill pack entries.
    """
    content = path.read_text(encoding="utf-8", errors="replace")
    result: dict = {}

    current_section = None
    current_pack = None
    current_pack_data: dict = {}

    for line in content.splitlines():
        stripped = line.strip()

        # Skip comments and empty lines
        if not stripped or stripped.startswith("#"):
            continue

        # Detect top-level section (no indent, ends with colon)
        if not line.startswith(" ") and stripped.endswith(":") and not stripped.startswith("-"):
            key = stripped.rstrip(":")
            if key in SKILL_PACK_SECTIONS:
                current_section = key
                result[current_section] = {}
                current_pack = None
            else:
                current_section = None
                current_pack = None
            continue

        # Inside a skill pack section, detect pack name (2-space indent)
        if current_section and re.match(r"^  [a-z]", line) and ":" in stripped:
            # Save previous pack
            if current_pack and current_pack_data:
                result[current_section][current_pack] = current_pack_data

            current_pack = stripped.split(":")[0].strip()
            current_pack_data = {}
            continue

        # Inside a pack, extract key-value pairs (4+ space indent)
        if current_section and current_pack and ":" in stripped:
            parts = stripped.split(":", 1)
            key = parts[0].strip()
            val = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""
            if key and val:
                current_pack_data[key] = val

    # Save last pack
    if current_section and current_pack and current_pack_data:
        result[current_section][current_pack] = current_pack_data

    return result


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Normalize a name by converting hyphens to underscores and lowercasing."""
    return name.lower().replace("-", "_").replace(" ", "_")


def validate_skill_pack(
    section: str,
    pack_key: str,
    pack_data: dict,
    errors: list[str],
    warnings: list[str],
    ecosystem_packs: set[str],
    strict: bool = False,
) -> bool:
    """
    Validate a single skill pack entry.
    Returns True if the pack passes all checks, False otherwise.
    """
    pack_passed = True
    pack_id = pack_data.get("id", "")
    pack_name = pack_data.get("name", "")
    manifest_path = pack_data.get("manifest_path", "")

    label = f"{section}/{pack_key}"

    # -----------------------------------------------------------------------
    # Check 1: manifest_path exists
    # -----------------------------------------------------------------------
    pack_status = pack_data.get("status", "")
    if manifest_path:
        full_path = PROJECT_ROOT / manifest_path
        if not full_path.exists():
            if pack_status == "designed" and not strict:
                warnings.append(
                    f"  WARN [{label}] manifest_path does not exist (expected — status is 'designed'): {manifest_path}"
                )
            else:
                errors.append(
                    f"  FAIL [{label}] manifest_path does not exist: {manifest_path}"
                )
                pack_passed = False
    else:
        errors.append(f"  FAIL [{label}] missing manifest_path field")
        pack_passed = False

    # -----------------------------------------------------------------------
    # Check 2: ID/name consistency (no hyphen/underscore mismatch)
    # -----------------------------------------------------------------------
    if pack_id:
        # The key in the YAML should match the id field
        if pack_key != pack_id:
            errors.append(
                f"  FAIL [{label}] key '{pack_key}' does not match id '{pack_id}'"
            )
            pack_passed = False

        # Check for hyphen/underscore mismatch between id and name
        # The id uses underscores; the name should use the same base words
        if pack_name:
            normalized_id = normalize_name(pack_id)
            normalized_name = normalize_name(pack_name)
            # Extract just the base name (before parenthetical)
            base_name_match = re.match(r"([^(]+)", pack_name)
            if base_name_match:
                base_name = base_name_match.group(1).strip()
                normalized_base = normalize_name(base_name)
                # Check that id contains the base name components or vice versa
                id_parts = set(normalized_id.split("_"))
                name_parts = set(normalized_base.split("_"))
                # At least some overlap expected
                if not id_parts & name_parts:
                    warnings.append(
                        f"  WARN [{label}] id '{pack_id}' and name '{pack_name}' "
                        f"have no common terms (possible mismatch)"
                    )
    else:
        errors.append(f"  FAIL [{label}] missing id field")
        pack_passed = False

    # -----------------------------------------------------------------------
    # Check 3: Cross-reference with ecosystem skillpacks directory
    # -----------------------------------------------------------------------
    if ecosystem_packs and pack_id:
        normalized_pack_id = normalize_name(pack_id)
        found_in_ecosystem = False
        for ep in ecosystem_packs:
            if normalize_name(ep) == normalized_pack_id:
                found_in_ecosystem = True
                break
        if not found_in_ecosystem:
            # This is a warning, not an error -- ecosystem might use different names
            warnings.append(
                f"  WARN [{label}] id '{pack_id}' not found in ecosystem skillpacks/ directory"
            )

    # -----------------------------------------------------------------------
    # Check 4: certification fields present
    # -----------------------------------------------------------------------
    certification = pack_data.get("certification", None)
    if certification is None:
        # Check if certification keys exist as flat keys (basic parser)
        has_cert_keys = any(
            k.startswith("tc_0") for k in pack_data
        )
        if not has_cert_keys:
            warnings.append(
                f"  WARN [{label}] no certification fields found"
            )

    return pack_passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    strict = "--strict" in sys.argv

    if not REGISTRY_PATH.exists():
        print(f"ERROR: Registry file not found at {REGISTRY_PATH}")
        return 1

    print("=" * 72)
    print("  VALIDATE SKILL PACKS REGISTRY")
    print("=" * 72)
    print(f"  Registry: {REGISTRY_PATH.relative_to(PROJECT_ROOT)}")
    if not strict:
        print("  Mode: tolerant (missing manifests OK for 'designed' packs)")
        print("  Use --strict to fail on all missing manifests")
    else:
        print("  Mode: strict (all missing manifests are errors)")
    print()

    # Load registry
    data = load_yaml(REGISTRY_PATH)
    if not data:
        print("  ERROR: Failed to parse registry YAML or file is empty.")
        return 1

    # Load ecosystem skillpacks for cross-reference
    ecosystem_packs: set[str] = set()
    if ECOSYSTEM_SKILLPACKS.exists() and ECOSYSTEM_SKILLPACKS.is_dir():
        ecosystem_packs = {p.name for p in ECOSYSTEM_SKILLPACKS.iterdir()}
        print(f"  Ecosystem skillpacks found: {len(ecosystem_packs)}")
    else:
        # Also check for skill_packs directories in ecosystem
        alt_paths = [
            PROJECT_ROOT / "plan" / "temp_ecosystem_scan"
            / "aspire_ecosystem_v12.7_2026-02-03" / "platform" / "trust-spine"
            / "01_ORIGINAL_INPUTS" / "claude_handoff_4_0" / "phase2_integrations"
            / "skill_packs",
        ]
        for alt in alt_paths:
            if alt.exists() and alt.is_dir():
                ecosystem_packs = {p.name for p in alt.iterdir() if p.is_dir()}
                print(f"  Ecosystem skill_packs found (alt path): {len(ecosystem_packs)}")
                break

        if not ecosystem_packs:
            print("  Ecosystem skillpacks directory not found (cross-ref skipped)")

    print()

    errors: list[str] = []
    warnings: list[str] = []
    packs_checked = 0
    packs_passed = 0

    # Iterate over each skill pack section
    for section in SKILL_PACK_SECTIONS:
        section_data = data.get(section)
        if not section_data or not isinstance(section_data, dict):
            continue

        print(f"  Section: {section}")

        for pack_key, pack_data in section_data.items():
            if not isinstance(pack_data, dict):
                continue

            packs_checked += 1
            passed = validate_skill_pack(
                section, pack_key, pack_data, errors, warnings, ecosystem_packs,
                strict=strict,
            )
            status = "PASS" if passed else "FAIL"
            pack_name = pack_data.get("name", pack_key)
            print(f"    [{status}] {pack_key} ({pack_name})")

            if passed:
                packs_passed += 1

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print()
    print("-" * 72)
    print(f"  Skill packs checked: {packs_checked}")
    print(f"  Passed: {packs_passed}")
    print(f"  Failed: {packs_checked - packs_passed}")
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
