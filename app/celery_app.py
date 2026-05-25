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

# When TLS is in use (Upstash), we must pass ssl_cert_reqs=None so that
# Celery does not attempt to verify the certificate against a local CA bundle
# (Upstash uses a wildcard cert that Python's ssl module may not trust).
_SSL_OPTS: dict = {"ssl_cert_reqs": None} if _IS_TLS else {}

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
celery_app = Celery(
    "bmcc_bot",
    broker=_REDIS_URL,
    backend=_REDIS_URL,
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
