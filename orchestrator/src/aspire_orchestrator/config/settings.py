"""Environment-based configuration for the Aspire orchestrator."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Orchestrator configuration loaded from environment variables."""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # Policy eval Edge Function
    policy_eval_url: str = ""

    # LLM (for NeMo Guardrails + orchestrator reasoning)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3:8b"

    # Capability Tokens
    token_signing_key: str = ""  # HMAC-SHA256 key — MUST be set in production
    token_ttl_seconds: int = 45  # Default <60s per Law #5

    # Gateway
    gateway_url: str = "http://localhost:3100"

    # Domain Rail (S2S)
    domain_rail_url: str = "http://domain-rail.railway.internal"
    s2s_hmac_secret: str = ""

    model_config = {"env_prefix": "ASPIRE_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
