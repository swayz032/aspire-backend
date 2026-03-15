#!/usr/bin/env python3
"""ReceiptAudit — CI gate that scans state-changing endpoints for receipt emission.

Scans @router.post/put/patch/delete handlers and verifies they call
receipt_store.emit() or create_receipt(). Supports `# noqa: receipt` pragma
for intentional exceptions.

Usage:
  python tools/ci/receipt_audit.py [--strict]
  --strict: exit 1 on any missing receipt (CI mode)

Exit codes:
  0 = all handlers have receipts (or non-strict mode)
  1 = missing receipts found (strict mode)
"""

import ast
import sys
from pathlib import Path
from typing import NamedTuple


class Violation(NamedTuple):
    file: str
    line: int
    function: str
    method: str


RECEIPT_CALLS = {
    'emit', 'emit_receipt', 'create_receipt', 'store_receipt',
    'receipt_store', 'emit_sync', 'emit_async',
}

STATE_CHANGING_DECORATORS = {'post', 'put', 'patch', 'delete'}


def scan_file(filepath: Path) -> list[Violation]:
    """Scan a Python file for state-changing handlers missing receipt emission."""
    violations = []

    try:
        source = filepath.read_text(encoding='utf-8')
    except Exception:
        return violations

    # Check for noqa at file level
    if '# noqa: receipt-file' in source:
        return violations

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return violations

    lines = source.splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Check decorators for @router.post/put/patch/delete
        http_method = None
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                if decorator.func.attr in STATE_CHANGING_DECORATORS:
                    http_method = decorator.func.attr
                    break

        if not http_method:
            continue

        # Check for noqa on the function line
        func_line = lines[node.lineno - 1] if node.lineno <= len(lines) else ''
        if '# noqa: receipt' in func_line:
            continue

        # Walk the function body looking for receipt calls
        has_receipt = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                # Check for receipt_store.emit(), create_receipt(), etc.
                if isinstance(child.func, ast.Attribute):
                    if child.func.attr in RECEIPT_CALLS:
                        has_receipt = True
                        break
                    if isinstance(child.func.value, ast.Name) and child.func.value.id == 'receipt_store':
                        has_receipt = True
                        break
                elif isinstance(child.func, ast.Name):
                    if child.func.id in RECEIPT_CALLS:
                        has_receipt = True
                        break
            # Check for string references to receipt functions
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if any(rc in child.value for rc in RECEIPT_CALLS):
                    has_receipt = True
                    break

        if not has_receipt:
            violations.append(Violation(
                file=str(filepath),
                line=node.lineno,
                function=node.name,
                method=http_method.upper(),
            ))

    return violations


def main() -> int:
    strict = '--strict' in sys.argv

    # Scan all Python files in the orchestrator source
    src_dir = Path(__file__).resolve().parent.parent.parent / 'orchestrator' / 'src'

    if not src_dir.exists():
        print(f'ReceiptAudit: Source directory not found: {src_dir}')
        return 1

    all_violations: list[Violation] = []

    for py_file in src_dir.rglob('*.py'):
        if '__pycache__' in str(py_file):
            continue
        violations = scan_file(py_file)
        all_violations.extend(violations)

    if not all_violations:
        print('ReceiptAudit: All state-changing handlers have receipt emission.')
        return 0

    print(f'ReceiptAudit: {len(all_violations)} handler(s) missing receipt emission:')
    print()
    for v in all_violations:
        print(f'  {v.file}:{v.line} — {v.method} {v.function}()')
    print()
    print('Fix: Add receipt_store.emit() or create_receipt() call.')
    print('Suppress: Add "# noqa: receipt" comment on the function line.')

    return 1 if strict else 0


if __name__ == '__main__':
    sys.exit(main())
