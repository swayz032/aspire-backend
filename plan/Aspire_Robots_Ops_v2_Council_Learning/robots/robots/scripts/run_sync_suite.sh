#!/usr/bin/env bash
set -euo pipefail
CFG=${1:-robots.config.yaml}
python3 robots/run_suite.py --config "$CFG" --env staging --version-ref local
