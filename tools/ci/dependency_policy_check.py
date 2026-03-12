from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / ".github" / "dependency-policy.yml"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def _load_policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def _matches_path(changed_file: str, pattern: str) -> bool:
    normalized = changed_file.replace("\\", "/")
    check = pattern.replace("\\", "/")
    if check.endswith("/"):
        return normalized.startswith(check)
    return normalized == check or normalized.startswith(f"{check}/")


def _collect_changed_files(base: str, head: str) -> list[str]:
    if base == head:
        return []
    try:
        stdout = _git("diff", "--name-only", base, head)
    except RuntimeError:
        return []
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _collect_diff_text(base: str, head: str, files: list[str]) -> str:
    if not files or base == head:
        return ""
    try:
        return _git("diff", "--unified=0", base, head, "--", *files)
    except RuntimeError:
        return ""


def _emit(outputs: dict[str, str]) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{key}={value}" for key, value in outputs.items()]
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(f"{line}\n")
    else:
        sys.stdout.write("\n".join(lines))
        sys.stdout.write("\n")


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: dependency_policy_check.py <base> <head>")

    base, head = sys.argv[1], sys.argv[2]
    policy = _load_policy()
    changed_files = _collect_changed_files(base, head)
    changed_text = "\n".join(changed_files)
    diff_text = _collect_diff_text(base, head, changed_files)

    dependency_files = []
    protected_dependencies = policy["package_groups"]["backend_framework"]["dependencies"]
    outputs: dict[str, str] = {}

    for name, config in policy["domains"].items():
        touched = any(
            _matches_path(changed_file, path)
            for changed_file in changed_files
            for path in config.get("paths", [])
        )
        outputs[name] = str(touched).lower()
        dependency_files.extend(config.get("manifests", []))

    dependency_paths_touched = any(
        _matches_path(changed_file, dependency_file)
        for changed_file in changed_files
        for dependency_file in dependency_files
    )
    outputs["dependency_files_touched"] = str(dependency_paths_touched).lower()

    protected_changed = any(
        dep in diff_text
        for dep in protected_dependencies
    )
    outputs["protected_backend_framework_changed"] = str(protected_changed).lower()

    cross_surface = any(
        outputs[key] == "true"
        for key in ("shared_contracts",)
    )
    outputs["cross_surface_required"] = str(cross_surface).lower()
    outputs["changed_files_json"] = json.dumps(changed_files)
    outputs["changed_summary"] = changed_text[:8000]

    _emit(outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
