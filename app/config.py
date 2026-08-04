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

    # Cron endpoint secret — cron-job.org sends this as "Authorization: Bearer <secret>"
    # to authenticate the /cron/sync-all keep-alive endpoint.
    cron_secret: str = ""

    # Telegram user ID to DM with system-health alerts (weekly reconcile
    # drift found, Strava webhook subscription mismatch). Optional — these
    # checks still run and log either way, but can't page anyone without
    # this set.
    admin_telegram_id: int | None = None

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

    @property
    def strava_webhook_valid_callback_urls(self) -> set[str]:
        """Both are legitimate: /webhook is what the live Strava
        subscription is actually registered to (see the comment above the
        /webhook alias routes in app/main.py — it predates the
        /strava/webhook prefix and was never re-registered rather than
        risk losing the subscription), /strava/webhook is the "canonical"
        namespaced path. The same handlers serve both, so either one
        being registered is healthy — comparing against just one of them
        produces a permanent false-positive mismatch warning."""
        return {self.strava_webhook_callback_url, f"{self.base_url}/webhook"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance. Import and call this everywhere."""
    return Settings()
