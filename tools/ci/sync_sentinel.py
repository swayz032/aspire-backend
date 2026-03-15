#!/usr/bin/env python3
"""SyncSentinel — Cross-repo dependency version drift detection.

Compares shared dependency versions across Desktop + Admin Portal to detect
version mismatches that could cause runtime incompatibilities.

Usage:
  python tools/ci/sync_sentinel.py [--strict]
  --strict: exit 1 on any version mismatch

Exit codes:
  0 = all shared deps match (or non-strict mode)
  1 = version drift detected (strict mode)
"""

import json
import sys
from pathlib import Path
from typing import NamedTuple


class Drift(NamedTuple):
    package: str
    desktop_version: str
    admin_version: str


# Shared dependencies that MUST match across repos
TRACKED_DEPS = [
    '@supabase/supabase-js',
    'react',
    'react-dom',
    'typescript',
    'zod',
]


def load_package_json(path: Path) -> dict:
    """Load and parse a package.json file."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f'  WARN  Cannot read {path}: {e}')
        return {}


def get_dep_version(pkg: dict, name: str) -> str:
    """Get a dependency version from package.json (deps or devDeps)."""
    deps = pkg.get('dependencies', {})
    dev_deps = pkg.get('devDependencies', {})
    return deps.get(name, dev_deps.get(name, ''))


def main() -> int:
    strict = '--strict' in sys.argv
    # tools/ci/sync_sentinel.py is in backend/tools/ci/
    # Workspace root is 3 levels up (backend -> myapp)
    backend_root = Path(__file__).resolve().parent.parent.parent
    workspace_root = backend_root.parent  # myapp/

    desktop_pkg_path = workspace_root / 'Aspire-desktop' / 'package.json'
    admin_pkg_path = workspace_root / 'Aspire-Admin-Portal' / 'package.json'

    print('SyncSentinel: Checking shared dependency versions...')
    print()

    desktop_pkg = load_package_json(desktop_pkg_path)
    admin_pkg = load_package_json(admin_pkg_path)

    if not desktop_pkg or not admin_pkg:
        print('SyncSentinel: Could not read one or both package.json files.')
        return 1

    drifts: list[Drift] = []

    for dep in TRACKED_DEPS:
        desktop_ver = get_dep_version(desktop_pkg, dep)
        admin_ver = get_dep_version(admin_pkg, dep)

        if not desktop_ver and not admin_ver:
            continue

        if not desktop_ver:
            print(f'  SKIP  {dep} — only in Admin Portal ({admin_ver})')
            continue

        if not admin_ver:
            print(f'  SKIP  {dep} — only in Desktop ({desktop_ver})')
            continue

        if desktop_ver == admin_ver:
            print(f'  OK    {dep} — {desktop_ver}')
        else:
            print(f'  DRIFT {dep} — Desktop: {desktop_ver}, Admin: {admin_ver}')
            drifts.append(Drift(dep, desktop_ver, admin_ver))

    print()

    if not drifts:
        print('SyncSentinel: All shared dependencies are in sync.')
        return 0

    print(f'SyncSentinel: {len(drifts)} dependency drift(s) detected!')
    print()
    for d in drifts:
        print(f'  {d.package}:')
        print(f'    Desktop:      {d.desktop_version}')
        print(f'    Admin Portal: {d.admin_version}')
    print()
    print('Fix: Align versions in both package.json files.')

    return 1 if strict else 0


if __name__ == '__main__':
    sys.exit(main())
