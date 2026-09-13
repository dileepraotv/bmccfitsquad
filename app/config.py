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

    # Telegram user ID(s) to DM with system-health alerts (weekly reconcile
    # drift found, Strava webhook subscription mismatch, new-user join
    # requests). Optional — these checks still run and log either way, but
    # can't page anyone without at least one of these set. A second admin
    # is optional; either one can Approve/Reject join requests, and both
    # receive every alert.
    admin_telegram_id: int | None = None
    admin_telegram_id_2: int | None = None

    @property
    def admin_telegram_ids(self) -> list[int]:
        return [i for i in (self.admin_telegram_id, self.admin_telegram_id_2) if i is not None]

    # Hub-relayed webhook delivery (see app/strava/webhook.py's module
    # docstring) — Strava allows only one webhook subscription per app, so
    # at any given time it points at either this app directly or at the
    # hub, which relays here. Both left empty means "hub relay isn't set
    # up yet"; the direct POST /strava/webhook path keeps working either way.
    hub_forward_secret: str = ""        # shared bearer secret, must match hub's config
    hub_webhook_callback_url: str = ""  # e.g. https://hub.beyondmiles.cc/api/strava/webhook

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
        """/webhook and /strava/webhook are both legitimate direct-delivery
        URLs: /webhook is what the live Strava subscription is actually
        registered to historically (see the comment above the /webhook
        alias routes in app/main.py — it predates the /strava/webhook
        prefix and was never re-registered rather than risk losing the
        subscription), /strava/webhook is the "canonical" namespaced path.
        hub_webhook_callback_url is a third legitimate option once the hub
        relay is in use (see app/strava/webhook.py) — Strava's subscription
        then points there instead, and the hub forwards events here.
        Comparing against just one of these produces a permanent
        false-positive mismatch warning the moment any of the others is
        what's actually registered."""
        urls = {self.strava_webhook_callback_url, f"{self.base_url}/webhook"}
        if self.hub_webhook_callback_url:
            urls.add(self.hub_webhook_callback_url)
        return urls


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance. Import and call this everywhere."""
    return Settings()
