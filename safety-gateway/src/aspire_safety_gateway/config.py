from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8787
    api_key: str = ""
    mode: str = "local"  # local | nemo
    fail_closed: bool = True
    max_payload_chars: int = 20000
    nemo_config_path: str = "config/nemo/default"
    nemo_refusal_contains: str = "I can’t help with that request."

    model_config = {"env_prefix": "ASPIRE_SAFETY_GATEWAY_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
