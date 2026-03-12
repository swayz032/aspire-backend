from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


REQUIRED_KEYS = (
    "ASPIRE_ENV",
    "ASPIRE_SAFETY_GATEWAY_MODE",
    "ASPIRE_SAFETY_GATEWAY_API_KEY",
    "ASPIRE_SAFETY_GATEWAY_SHARED_SECRET",
    "ASPIRE_TOKEN_SIGNING_KEY",
    "ASPIRE_OPENAI_API_KEY",
    "ASPIRE_SUPABASE_URL",
    "ASPIRE_SUPABASE_SERVICE_ROLE_KEY",
    "ASPIRE_LANGGRAPH_CHECKPOINTER",
)


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def wait_for_http(url: str, *, timeout_seconds: float = 90.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code == 200:
                return
        except Exception as exc:  # pragma: no cover - transient readiness path
            last_error = exc
        time.sleep(2.0)
    raise SystemExit(f"Timed out waiting for {url}: {last_error}")


def validate(env_values: dict[str, str]) -> None:
    missing = [key for key in REQUIRED_KEYS if not env_values.get(key)]
    if missing:
        raise SystemExit(f"Missing required env keys: {', '.join(missing)}")

    if len(env_values["ASPIRE_TOKEN_SIGNING_KEY"]) < 32:
        raise SystemExit("ASPIRE_TOKEN_SIGNING_KEY must be at least 32 characters")

    if env_values["ASPIRE_SAFETY_GATEWAY_API_KEY"] != env_values["ASPIRE_SAFETY_GATEWAY_SHARED_SECRET"]:
        raise SystemExit("ASPIRE_SAFETY_GATEWAY_API_KEY and ASPIRE_SAFETY_GATEWAY_SHARED_SECRET must match")

    checkpointer = env_values.get("ASPIRE_LANGGRAPH_CHECKPOINTER", "").strip().lower()
    if checkpointer == "postgres" and not env_values.get("ASPIRE_LANGGRAPH_POSTGRES_DSN"):
        raise SystemExit("ASPIRE_LANGGRAPH_POSTGRES_DSN is required when ASPIRE_LANGGRAPH_CHECKPOINTER=postgres")

    if env_values.get("ASPIRE_ENV", "").strip().lower() == "production" and checkpointer != "postgres":
        raise SystemExit("Production deployment requires ASPIRE_LANGGRAPH_CHECKPOINTER=postgres")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight and deploy the orchestrator+safety Docker stack")
    parser.add_argument(
        "--env-file",
        default="infrastructure/docker/orchestrator-safety.env",
        help="Path to the Docker env file",
    )
    parser.add_argument(
        "--compose-file",
        default="infrastructure/docker/docker-compose.orchestrator-safety.prod.yml",
        help="Path to the compose file",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Skip the post-deploy smoke test",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    env_file = root / args.env_file
    compose_file = root / args.compose_file

    if not env_file.exists():
        raise SystemExit(f"Env file not found: {env_file}")
    if not compose_file.exists():
        raise SystemExit(f"Compose file not found: {compose_file}")

    env_values = load_env(env_file)
    validate(env_values)

    run(["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file), "config", "-q"], cwd=root)
    run(["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file), "up", "--build", "-d"], cwd=root)

    if not args.skip_smoke:
        wait_for_http("http://127.0.0.1:8787/healthz")
        wait_for_http("http://127.0.0.1:8000/healthz")
        smoke_env = os.environ.copy()
        smoke_env["PYTHONPATH"] = str(root / "orchestrator" / "src")
        run(
            [
                str(root / "orchestrator" / ".venv" / "Scripts" / "python.exe"),
                str(root / "scripts" / "smoke_orchestrator_safety_remote.py"),
            ],
            cwd=root,
            env=smoke_env,
        )

    print("Deployment preflight and stack startup completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
