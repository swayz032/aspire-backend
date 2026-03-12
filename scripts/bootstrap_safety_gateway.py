from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SAFETY_GATEWAY_DIR = REPO_ROOT / "safety-gateway"
DEFAULT_VENV_DIR = SAFETY_GATEWAY_DIR / ".venv313"


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def resolve_python(python_arg: str | None) -> list[str]:
    if python_arg:
        return [python_arg]

    if sys.platform.startswith("win"):
        py_launcher = shutil.which("py")
        if py_launcher:
            return [py_launcher, "-3.13"]

    python313 = shutil.which("python3.13")
    if python313:
        return [python313]

    raise SystemExit(
        "Python 3.13 interpreter not found. Install Python 3.13 or pass --python explicitly."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the Aspire Safety Gateway on Python 3.13")
    parser.add_argument("--python", help="Explicit Python 3.13 interpreter path")
    parser.add_argument(
        "--venv-dir",
        default=str(DEFAULT_VENV_DIR),
        help="Virtualenv directory to create/use",
    )
    parser.add_argument(
        "--mode",
        choices=("editable", "wheel"),
        default="editable",
        help="Install mode for the safety-gateway package",
    )
    args = parser.parse_args()

    python_cmd = resolve_python(args.python)
    venv_dir = Path(args.venv_dir)

    run([*python_cmd, "-m", "venv", str(venv_dir)], cwd=SAFETY_GATEWAY_DIR)

    if sys.platform.startswith("win"):
        python_bin = venv_dir / "Scripts" / "python.exe"
    else:
        python_bin = venv_dir / "bin" / "python"

    if not python_bin.exists():
        raise SystemExit(f"Expected venv python at {python_bin} but it was not created.")

    run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"], cwd=SAFETY_GATEWAY_DIR)

    install_target = ".[dev]" if args.mode == "editable" else ".[dev]"
    run([str(python_bin), "-m", "pip", "install", "-e", install_target], cwd=SAFETY_GATEWAY_DIR)

    env_example = SAFETY_GATEWAY_DIR / ".env.example"
    env_file = SAFETY_GATEWAY_DIR / ".env"
    if env_example.exists() and not env_file.exists():
        env_file.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Bootstrapped safety gateway in {venv_dir}")
    print(f"Run with: {python_bin} -m uvicorn aspire_safety_gateway.app:app --host 0.0.0.0 --port 8787")


if __name__ == "__main__":
    main()
