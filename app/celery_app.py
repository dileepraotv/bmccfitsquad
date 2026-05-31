"""Celery application instance.

Broker uses Upstash Redis (rediss://).  The result backend is intentionally
disabled (task_ignore_result=True) because nothing in this codebase reads
task results — storing them wastes ~4-8 Redis commands per task against the
Upstash free-tier request limit.

Start the worker locally with:
    celery -A app.celery_app worker --loglevel=info --pool=solo --concurrency=1
"""
from celery import Celery

from app.config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# Broker URL — Upstash requires TLS (rediss://) with ssl_cert_reqs=CERT_NONE
# ---------------------------------------------------------------------------
_REDIS_URL: str = settings.redis_url
_IS_TLS: bool   = _REDIS_URL.startswith("rediss://")


def _tls_url(url: str) -> str:
    """Append ssl_cert_reqs=CERT_NONE to rediss:// URLs if not already present."""
    if not url.startswith("rediss://"):
        return url
    sep = "&" if "?" in url else "?"
    if "ssl_cert_reqs" in url:
        return url
    return f"{url}{sep}ssl_cert_reqs=CERT_NONE"


_BROKER_URL = _tls_url(_REDIS_URL)
_SSL_OPTS: dict = {"ssl_cert_reqs": "CERT_NONE"} if _IS_TLS else {}

# ---------------------------------------------------------------------------
# Application — no result backend to eliminate backend Redis traffic
# ---------------------------------------------------------------------------
celery_app = Celery(
    "bmcc_bot",
    broker=_BROKER_URL,
    backend=None,           # results not used — saves ~4-8 Redis ops per task
    include=["app.tasks"],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Never store results — biggest Redis saver
    task_ignore_result=True,

    # Reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Retry policy
    task_default_retry_delay=30,
    task_max_retries=3,

    # Reduce broker polling frequency — default is 1s; 4s cuts idle broker
    # pings by 4x with negligible latency impact for notification tasks.
    broker_transport_options={
        "visibility_timeout": 3600,
        "polling_interval": 4,
    },

    # TLS for Upstash broker
    broker_use_ssl=_SSL_OPTS or None,

    # Retry broker connection on startup (suppresses deprecation warning)
    broker_connection_retry_on_startup=True,
)
