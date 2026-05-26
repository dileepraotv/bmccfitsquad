"""Celery application instance.

Broker & backend both use the Redis URL from config.  Upstash provides TLS
connections via ``rediss://`` — this module detects that scheme and adds the
required SSL options automatically.

Start the worker locally with:
    celery -A app.celery_app worker --loglevel=info

On Railway, add a separate service or Procfile line:
    worker: celery -A app.celery_app worker --loglevel=info --concurrency=2
"""
from celery import Celery

from app.config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# Broker / backend URL
# ---------------------------------------------------------------------------
# Upstash Redis requires TLS — the URL begins with  rediss://
# Plain self-hosted Redis uses redis://
# Celery's redis transport supports both schemes natively.
_REDIS_URL: str = settings.redis_url
_IS_TLS: bool = _REDIS_URL.startswith("rediss://")

# Celery's Redis backend requires ssl_cert_reqs to be present both as a URL
# query parameter AND in the broker_use_ssl / redis_backend_use_ssl dicts.
# Append it to the URL so the backend transport parser is satisfied.
def _tls_url(url: str) -> str:
    if not url.startswith("rediss://"):
        return url
    sep = "&" if "?" in url else "?"
    if "ssl_cert_reqs" in url:
        return url
    return f"{url}{sep}ssl_cert_reqs=CERT_NONE"

_BROKER_URL  = _tls_url(_REDIS_URL)
_BACKEND_URL = _tls_url(_REDIS_URL)
_SSL_OPTS: dict = {"ssl_cert_reqs": "CERT_NONE"} if _IS_TLS else {}

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
celery_app = Celery(
    "bmcc_bot",
    broker=_BROKER_URL,
    backend=_BACKEND_URL,
    include=["app.tasks"],
)

celery_app.conf.update(
    # Serialisation — JSON is safe, human-readable, and firewall-friendly
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Reliability
    task_acks_late=True,            # only ack after the task completes
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,   # one task at a time per worker thread

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Result TTL — we don't need long-lived task results
    result_expires=3_600,

    # Default retry policy (individual tasks may override)
    task_default_retry_delay=30,
    task_max_retries=3,

    # TLS options for Upstash (ignored when _SSL_OPTS is empty)
    broker_use_ssl=_SSL_OPTS or None,
    redis_backend_use_ssl=_SSL_OPTS or None,
)
