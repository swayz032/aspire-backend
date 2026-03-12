from __future__ import annotations

import argparse
from pathlib import Path
import secrets


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a Docker env file for the orchestrator+safety stack")
    parser.add_argument(
        "--output",
        default="infrastructure/docker/orchestrator-safety.env",
        help="Path to the generated env file",
    )
    parser.add_argument(
        "--environment",
        default="production",
        choices=("development", "production"),
        help="Set ASPIRE_ENV in the generated file",
    )
    parser.add_argument(
        "--checkpointer",
        default="memory",
        choices=("memory", "postgres"),
        help="Set ASPIRE_LANGGRAPH_CHECKPOINTER in the generated file",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    orchestrator_env = load_env(root / "orchestrator" / ".env")
    safety_env = load_env(root / "safety-gateway" / ".env")
    gateway_secret = safety_env.get("ASPIRE_SAFETY_GATEWAY_API_KEY", "")
    if not gateway_secret and args.environment == "development":
        gateway_secret = secrets.token_urlsafe(32)

    merged = {
        "ASPIRE_ENV": args.environment,
        "ASPIRE_SAFETY_GATEWAY_MODE": safety_env.get("ASPIRE_SAFETY_GATEWAY_MODE", "local"),
        "ASPIRE_SAFETY_GATEWAY_API_KEY": gateway_secret,
        "ASPIRE_SAFETY_GATEWAY_SHARED_SECRET": gateway_secret,
        "ASPIRE_SAFETY_GATEWAY_TIMEOUT_SECONDS": orchestrator_env.get("ASPIRE_SAFETY_GATEWAY_TIMEOUT_SECONDS", "5"),
        "ASPIRE_TOKEN_SIGNING_KEY": orchestrator_env.get("ASPIRE_TOKEN_SIGNING_KEY", ""),
        "ASPIRE_OPENAI_API_KEY": orchestrator_env.get("ASPIRE_OPENAI_API_KEY", orchestrator_env.get("OPENAI_API_KEY", "")),
        "ASPIRE_OPENAI_BASE_URL": orchestrator_env.get("ASPIRE_OPENAI_BASE_URL", orchestrator_env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")),
        "ASPIRE_SUPABASE_URL": orchestrator_env.get("ASPIRE_SUPABASE_URL", orchestrator_env.get("SUPABASE_URL", "")),
        "ASPIRE_SUPABASE_SERVICE_ROLE_KEY": orchestrator_env.get(
            "ASPIRE_SUPABASE_SERVICE_ROLE_KEY",
            orchestrator_env.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        ),
        "ASPIRE_LANGGRAPH_CHECKPOINTER": args.checkpointer,
        "ASPIRE_LANGGRAPH_POSTGRES_DSN": orchestrator_env.get("ASPIRE_LANGGRAPH_POSTGRES_DSN", ""),
        "ASPIRE_GATEWAY_URL": orchestrator_env.get("ASPIRE_GATEWAY_URL", orchestrator_env.get("GATEWAY_URL", "")),
        "ASPIRE_POLICY_EVAL_URL": orchestrator_env.get("ASPIRE_POLICY_EVAL_URL", ""),
        "ASPIRE_CREDENTIAL_STRICT_MODE": orchestrator_env.get("ASPIRE_CREDENTIAL_STRICT_MODE", "true" if args.environment == "production" else "false"),
        "OPENAI_API_KEY": safety_env.get(
            "OPENAI_API_KEY",
            orchestrator_env.get("ASPIRE_OPENAI_API_KEY", orchestrator_env.get("OPENAI_API_KEY", "")),
        ),
    }

    output_path = root / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"{key}={value}" for key, value in merged.items()]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
