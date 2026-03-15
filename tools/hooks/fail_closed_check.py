#!/usr/bin/env python3
"""GovGuard: Fail-Closed Check Hook

Scans Python files for bare except blocks that silently swallow errors.
Law #3: Fail Closed — missing permission, policy, or verification = deny execution.

Usage: python tools/hooks/fail_closed_check.py [files...]
Exit code 1 if bare except or except Exception with pass/continue found.
"""
import ast
import sys
from pathlib import Path


NOQA_MARKER = '# noqa: fail-closed'


def check_file(filepath: str) -> list[str]:
    """Check a Python file for fail-open patterns."""
    violations: list[str] = []
    try:
        source = Path(filepath).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return violations

    lines = source.split('\n')

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return violations

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        # Check for noqa suppression
        line_text = lines[node.lineno - 1] if node.lineno <= len(lines) else ''
        if NOQA_MARKER in line_text:
            continue

        # Bare except (except:) — always a violation
        if node.type is None:
            violations.append(
                f'{filepath}:{node.lineno}: Bare `except:` clause — use specific exception types (Law #3)'
            )
            continue

        # except Exception with only pass/continue in body — silently swallowing
        if isinstance(node.type, ast.Name) and node.type.id == 'Exception':
            body = node.body
            if len(body) == 1:
                stmt = body[0]
                if isinstance(stmt, ast.Pass):
                    violations.append(
                        f'{filepath}:{node.lineno}: `except Exception: pass` — silently swallows errors (Law #3)'
                    )
                elif isinstance(stmt, ast.Continue):
                    violations.append(
                        f'{filepath}:{node.lineno}: `except Exception: continue` — silently swallows errors (Law #3)'
                    )

    return violations


def main() -> int:
    files = sys.argv[1:]
    if not files:
        return 0

    all_violations: list[str] = []

    for filepath in files:
        if filepath.endswith('.py'):
            all_violations.extend(check_file(filepath))

    if all_violations:
        print('GovGuard: Fail-Closed Check FAILED (Law #3: Fail Closed)')
        print('=' * 60)
        for v in all_violations:
            print(f'  {v}')
        print()
        print('Fix: Handle errors explicitly — log, raise, or return an error response.')
        print('Suppress with: # noqa: fail-closed')
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
