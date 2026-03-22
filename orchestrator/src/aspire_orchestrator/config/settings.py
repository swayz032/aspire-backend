"""Environment-based configuration for the Aspire orchestrator."""

import os

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

    # --- LLM Configuration ---
    # NeMo Guardrails safety gate (Ollama llama3:8b for local dev)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3:8b"
    safety_gateway_mode: str = "local"  # local | remote | off
    safety_gateway_url: str = ""
    safety_gateway_timeout_seconds: float = 5.0
    safety_gateway_fail_closed: bool = True
    safety_gateway_shared_secret: str = ""

    # Ava Brain intent classification + parameter extraction
    # Dev: GPT-5-mini (via OpenAI API or Ollama proxy)
    # Prod: GPT-5.2 (via OpenAI API)
    ava_llm_provider: str = "openai"  # "openai" | "ollama"
    ava_llm_model: str = "gpt-5-mini"  # "gpt-5-mini" (dev) | "gpt-5.2" (prod)
    openai_api_key: str = ""  # Required for production
    openai_base_url: str = "https://api.openai.com/v1"
    ava_llm_temperature: float = 0.4  # Slightly creative for natural conversation
    ava_llm_max_tokens: int = 4096

    # --- Phase 3: LLM Router (3-tier model routing) ---
    router_model_classifier: str = "gpt-5-mini"     # CHEAP_CLASSIFIER profile
    router_model_general: str = "gpt-5"             # FAST_GENERAL profile
    router_model_reasoner: str = "gpt-5.2"          # PRIMARY_REASONER profile
    router_model_high_risk: str = "gpt-5.2"         # HIGH_RISK_GUARD profile
    openai_use_chat_fallback: bool = True           # Legacy fallback if Responses API fails
    model_fallback_map: str = ""                    # Optional JSON map for model failover

    # --- LangGraph persistence ---
    langgraph_checkpointer: str = "memory"          # memory | postgres
    langgraph_postgres_dsn: str = ""                # Required when checkpointer=postgres

    # Capability Tokens (Law #3: fail-closed — sentinel value causes HMAC to fail if not overridden)
    token_signing_key: str = "UNCONFIGURED-FAIL-CLOSED"  # MUST be overridden via ASPIRE_TOKEN_SIGNING_KEY
    token_ttl_seconds: int = 45  # Default <60s per Law #5

    # Gateway
    gateway_url: str = "http://localhost:3100"

    # Domain Rail (S2S)
    domain_rail_url: str = "http://domain-rail.railway.internal"
    s2s_hmac_secret: str = ""

    # --- Provider API Keys (loaded from env, never hardcoded — Law #9) ---
    stripe_api_key: str = ""
    brave_api_key: str = ""
    tavily_api_key: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    pandadoc_api_key: str = ""
    pandadoc_webhook_secret: str = ""
    stripe_webhook_secret: str = ""
    pandadoc_credential_last_rotated: str | None = None  # ISO8601 date — rotation policy is 30 days
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""

    # Security — credential rotation enforcement (Law #3: fail-closed default)
    credential_strict_mode: bool = True  # Fail-closed; set ASPIRE_CREDENTIAL_STRICT_MODE=0 to disable in dev

    # --- Adam Research geo/places provider keys ---
    google_maps_api_key: str = ""
    tomtom_api_key: str = ""
    here_api_key: str = ""
    foursquare_api_key: str = ""
    mapbox_access_token: str = ""

    # --- Tec Documents (S3) provider keys ---
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_region: str = "us-east-1"

    # --- Plaid provider keys ---
    plaid_client_id: str = ""
    plaid_secret: str = ""

    # --- Provider OAuth2 (client credentials for token refresh) ---
    quickbooks_client_id: str = ""
    quickbooks_client_secret: str = ""
    quickbooks_base_url: str = ""  # Default: sandbox. Set to prod URL in production.
    gusto_client_id: str = ""
    gusto_client_secret: str = ""

    # --- Clara RAG Knowledge Base ---
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1536  # Reduced from 3072 for cost savings (99% quality retention)
    embedding_batch_size: int = 50
    rag_max_chunks_per_query: int = 10
    rag_min_similarity: float = 0.3
    rag_vector_weight: float = 0.7
    rag_text_weight: float = 0.3
    retrieval_min_grounding_score: float = 0.55
    retrieval_router_cache_ttl_seconds: int = 60
    retrieval_router_cache_max_entries: int = 500
    task_queue_max_concurrent: int = 20
    task_queue_max_pending: int = 500

    # --- Timeouts ---
    openai_timeout_seconds: int = 15  # Railway adds latency; 8s was too short

    # --- Ava v1.5 Features ---
    ava_user_prompt_version: str | None = None     # AVA_USER_PROMPT_VERSION env var
    ava_admin_prompt_version: str | None = None    # AVA_ADMIN_PROMPT_VERSION env var
    ava_safe_mode: bool = False                    # AVA_SAFE_MODE=1 for incident operation

    model_config = {"env_prefix": "ASPIRE_", "extra": "ignore"}

def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


_ENV_FILE = ".env" if _is_truthy(os.getenv("ASPIRE_ENABLE_LOCAL_DOTENV")) else None
settings = Settings(_env_file=_ENV_FILE)


def resolve_openai_api_key() -> str:
    """Resolve OpenAI key from authoritative runtime sources.

    Precedence:
    1) OPENAI_API_KEY (server-side secret manager / platform env)
    2) ASPIRE_OPENAI_API_KEY (legacy compatibility)
    3) settings.openai_api_key (last-resort fallback)
    """
    return (
        (os.getenv("OPENAI_API_KEY") or "").strip()
        or (os.getenv("ASPIRE_OPENAI_API_KEY") or "").strip()
        or (settings.openai_api_key or "").strip()
    )
