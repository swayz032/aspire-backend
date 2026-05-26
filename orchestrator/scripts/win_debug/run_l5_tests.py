"""Focused L-5 pytest runner with single-file rootdir to skip tree-wide discovery.
Bypasses 20-min pytest discovery overhead on Windows — use when full suite hangs.
Run: `python scripts/win_debug/run_l5_tests.py`"""
import sys
import time
from pathlib import Path

# Add src/ to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

# Run pytest scoped to one test file
import pytest

t0 = time.time()
exit_code = pytest.main([
    "src/aspire_orchestrator/services/blueprint/semantic_edges/generators/test_locality_generators.py",
    "-q",
    "--tb=line",
    "--no-header",
    "-p", "no:cacheprovider",
    "--rootdir", "src/aspire_orchestrator/services/blueprint/semantic_edges",
])
elapsed = time.time() - t0
print(f"\nDONE exit_code={exit_code} elapsed={elapsed:.1f}s")

# Write a marker file we can check
Path("_l5_test_result.txt").write_text(f"exit_code={exit_code}\nelapsed_seconds={elapsed:.1f}\n")
