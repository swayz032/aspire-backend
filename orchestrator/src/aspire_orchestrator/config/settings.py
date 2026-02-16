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

    # --- LLM Configuration ---
    # NeMo Guardrails safety gate (Ollama llama3:8b for local dev)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3:8b"

    # Ava Brain intent classification + parameter extraction
    # Dev: GPT-5-mini (via OpenAI API or Ollama proxy)
    # Prod: GPT-5.2 (via OpenAI API)
    ava_llm_provider: str = "openai"  # "openai" | "ollama"
    ava_llm_model: str = "gpt-5-mini"  # "gpt-5-mini" (dev) | "gpt-5.2" (prod)
    openai_api_key: str = ""  # Required for production
    openai_base_url: str = "https://api.openai.com/v1"
    ava_llm_temperature: float = 0.0  # Deterministic for intent classification
    ava_llm_max_tokens: int = 1024

    # --- Phase 3: LLM Router (3-tier model routing) ---
    router_model_classifier: str = "gpt-5-mini"     # CHEAP_CLASSIFIER profile
    router_model_general: str = "gpt-5"             # FAST_GENERAL profile
    router_model_reasoner: str = "gpt-5.2"          # PRIMARY_REASONER profile
    router_model_high_risk: str = "gpt-5.2"         # HIGH_RISK_GUARD profile

    # Capability Tokens
    token_signing_key: str = ""  # HMAC-SHA256 key — MUST be set in production
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
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""

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

    # --- Finn Money Desk provider keys ---
    moov_api_key: str = ""
    plaid_client_id: str = ""
    plaid_secret: str = ""

    # --- Provider OAuth2 (client credentials for token refresh) ---
    quickbooks_client_id: str = ""
    quickbooks_client_secret: str = ""
    quickbooks_base_url: str = ""  # Default: sandbox. Set to prod URL in production.
    gusto_client_id: str = ""
    gusto_client_secret: str = ""
    moov_client_id: str = ""
    moov_client_secret: str = ""

    model_config = {"env_prefix": "ASPIRE_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
