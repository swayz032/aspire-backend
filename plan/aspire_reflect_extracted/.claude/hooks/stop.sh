#!/usr/bin/env bash
set -euo pipefail

# Claude Code Stop Hook — Aspire Reflect (proposal-only)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Avoid recursion if Claude stops while running the stop hook
if [[ "${stop_hook_active:-}" == "1" ]]; then
  exit 0
fi
export stop_hook_active=1

SESSION_NOTES="${ROOT_DIR}/.claude/session-notes.md"

if [[ ! -f "${SESSION_NOTES}" ]]; then
  exit 0
fi

python3 "${ROOT_DIR}/scripts/reflect.py"       --skills-root "${ROOT_DIR}/skills"       --session-notes "${SESSION_NOTES}"       --out "${ROOT_DIR}/proposed"       || true

exit 0
