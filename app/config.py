from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    database_url: str

    # Redis (Upstash)
    redis_url: str

    # Strava OAuth
    strava_client_id: str
    strava_client_secret: str
    strava_webhook_verify_token: str

    # Telegram
    telegram_bot_token: str
    telegram_webhook_secret: str

    # Encryption key for storing Strava tokens at rest (Fernet key, base64-encoded)
    encryption_key: str

    # Deployment
    app_env: str = "development"
    base_url: str  # Public HTTPS URL used to register webhooks, e.g. https://myapp.up.railway.app

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def strava_redirect_uri(self) -> str:
        return f"{self.base_url}/strava/callback"

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.base_url}/telegram/webhook"

    @property
    def strava_webhook_callback_url(self) -> str:
        return f"{self.base_url}/strava/webhook"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance. Import and call this everywhere."""
    return Settings()
