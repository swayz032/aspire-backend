#!/usr/bin/env python3
"""GovGuard: Receipt Check Hook

Scans Python files for state-changing route handlers (@router.post/put/patch/delete)
and verifies they contain a receipt_store.emit() or createReceipt() call.

Law #2: Receipt for All Actions — every state change produces an immutable receipt.

Usage: python tools/hooks/receipt_check.py [files...]
Exit code 1 if any handler is missing receipt emission.
Supports `# noqa: receipt` to suppress for intentional exceptions.
"""
import ast
import sys
from pathlib import Path


ROUTE_DECORATORS = {'post', 'put', 'patch', 'delete'}
RECEIPT_CALLS = {'emit', 'createReceipt', 'create_receipt', 'receipt_store'}
NOQA_MARKER = '# noqa: receipt'


def check_python_file(filepath: str) -> list[str]:
    """Check a Python file for missing receipt emissions in state-changing handlers."""
    violations: list[str] = []
    try:
        source = Path(filepath).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return violations

    if NOQA_MARKER in source.split('\n')[0]:
        return violations  # File-level suppression

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return violations

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Check if function has a route decorator for state-changing methods
        is_state_changing = False
        for decorator in node.decorator_list:
            dec_name = ''
            if isinstance(decorator, ast.Call):
                if isinstance(decorator.func, ast.Attribute):
                    dec_name = decorator.func.attr
                elif isinstance(decorator.func, ast.Name):
                    dec_name = decorator.func.id
            elif isinstance(decorator, ast.Attribute):
                dec_name = decorator.attr

            if dec_name.lower() in ROUTE_DECORATORS:
                is_state_changing = True
                break

        if not is_state_changing:
            continue

        # Check line for noqa suppression
        line_text = source.split('\n')[node.lineno - 1] if node.lineno <= len(source.split('\n')) else ''
        if NOQA_MARKER in line_text:
            continue

        # Check if function body contains a receipt call
        has_receipt = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                call_name = ''
                if isinstance(child.func, ast.Attribute):
                    call_name = child.func.attr
                elif isinstance(child.func, ast.Name):
                    call_name = child.func.id
                if call_name in RECEIPT_CALLS:
                    has_receipt = True
                    break

        if not has_receipt:
            violations.append(
                f'{filepath}:{node.lineno}: {node.name}() — state-changing handler missing receipt emission (Law #2)'
            )

    return violations


def check_typescript_file(filepath: str) -> list[str]:
    """Basic TS check: scan for router.post/put/patch/delete without createReceipt."""
    violations: list[str] = []
    try:
        lines = Path(filepath).read_text(encoding='utf-8').split('\n')
    except (OSError, UnicodeDecodeError):
        return violations

    in_handler = False
    handler_start = 0
    handler_name = ''
    brace_depth = 0
    has_receipt = False

    for i, line in enumerate(lines, 1):
        if NOQA_MARKER in line:
            continue

        # Detect start of state-changing route handler
        for method in ROUTE_DECORATORS:
            pattern = f'router.{method}(' if 'router' in line.lower() else f'.{method}('
            if f'.{method}(' in line.lower() and ('router' in line.lower() or 'app' in line.lower()):
                in_handler = True
                handler_start = i
                handler_name = line.strip()[:80]
                brace_depth = 0
                has_receipt = False
                break

        if in_handler:
            brace_depth += line.count('{') - line.count('}')
            if 'createReceipt' in line or 'receipt_store' in line or 'emit(' in line:
                has_receipt = True

            if brace_depth <= 0 and i > handler_start:
                if not has_receipt:
                    violations.append(
                        f'{filepath}:{handler_start}: {handler_name} — state-changing handler missing receipt emission (Law #2)'
                    )
                in_handler = False

    return violations


def main() -> int:
    files = sys.argv[1:]
    if not files:
        return 0

    all_violations: list[str] = []

    for filepath in files:
        if filepath.endswith('.py'):
            all_violations.extend(check_python_file(filepath))
        elif filepath.endswith('.ts') or filepath.endswith('.tsx'):
            all_violations.extend(check_typescript_file(filepath))

    if all_violations:
        print('GovGuard: Receipt Check FAILED (Law #2: Receipt for All)')
        print('=' * 60)
        for v in all_violations:
            print(f'  {v}')
        print()
        print('Fix: Add receipt emission to each handler, or suppress with # noqa: receipt')
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
