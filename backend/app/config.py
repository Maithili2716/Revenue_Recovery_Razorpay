from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str
    database_url: str
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    demo_callback_url: str = "http://localhost:5173/recovery/demo-return"

    # LLM diagnosis — provider-neutral config.
    # Falls back to deterministic diagnosis when the API key is absent.
    grok_api_key: str | None = None
    grok_model: str = "grok-3-mini-fast"


settings = Settings()
