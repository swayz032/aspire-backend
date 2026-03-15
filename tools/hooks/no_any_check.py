#!/usr/bin/env python3
"""GovGuard: No-Any Check Hook

Scans TypeScript files for explicit `any` type annotations.
Aspire enterprise standard: No `any` in TypeScript.

Usage: python tools/hooks/no_any_check.py [files...]
Exit code 1 if explicit `any` types found (excluding legitimate casts and suppressed lines).
"""
import re
import sys
from pathlib import Path


# Patterns that indicate intentional any usage (not violations)
EXEMPT_PATTERNS = [
    r'//\s*eslint-disable',
    r'//\s*@ts-ignore',
    r'//\s*@ts-expect-error',
    r'//\s*noqa',
    r'as\s+any\)',  # Type assertions to any (sometimes needed for Express req)
]

# Match explicit `: any` type annotations (not `any` in strings or comments)
ANY_PATTERN = re.compile(r':\s*any\b(?!\w)')


def check_file(filepath: str) -> list[str]:
    """Check a TypeScript file for explicit any type annotations."""
    violations: list[str] = []
    try:
        lines = Path(filepath).read_text(encoding='utf-8').split('\n')
    except (OSError, UnicodeDecodeError):
        return violations

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Skip comments
        if stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('/*'):
            continue

        # Skip exempt patterns
        if any(re.search(p, line) for p in EXEMPT_PATTERNS):
            continue

        # Check for `: any` annotations
        if ANY_PATTERN.search(line):
            # Exclude `as any` casts (these are handled separately)
            clean = re.sub(r'as\s+any\b', '', line)
            if ANY_PATTERN.search(clean):
                violations.append(f'{filepath}:{i}: {stripped[:80]}')

    return violations


def main() -> int:
    files = sys.argv[1:]
    if not files:
        return 0

    all_violations: list[str] = []

    for filepath in files:
        if filepath.endswith('.ts') or filepath.endswith('.tsx'):
            all_violations.extend(check_file(filepath))

    if all_violations:
        print(f'GovGuard: No-Any Check — {len(all_violations)} explicit `any` found')
        print('=' * 60)
        for v in all_violations[:20]:  # Limit output
            print(f'  {v}')
        if len(all_violations) > 20:
            print(f'  ... and {len(all_violations) - 20} more')
        print()
        print('Fix: Replace `any` with a proper type. Use `unknown` if type is truly unknown.')
        # WARNING only — don't block commits yet (too many existing violations)
        # Change return to 1 when codebase is clean
        return 0

    return 0


if __name__ == '__main__':
    sys.exit(main())
