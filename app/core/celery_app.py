"""
app/core/celery_app.py

"""
from __future__ import annotations

import logging
import ssl
from logging.handlers import RotatingFileHandler

from celery import Celery
from celery.signals import after_setup_logger

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "job_finder",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=settings.TASK_RESULT_TTL_SECONDS,
    task_track_started=True,
    task_default_queue="default",
    broker_transport_options={
        "visibility_timeout": 3600,
        "socket_keepalive": True,
    },
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_heartbeat=30,
)

# rediss:// (TLS) connections to Upstash require explicit SSL config on
# BOTH the broker and result backend connections. redis.from_url(..., ssl_cert_reqs=None)
# in health.py works because it's a raw redis-py client — that kwarg does NOT
# propagate to Celery's own connection pool. Without this block, the worker's
# broker connection can fail cert verification silently and just never receive tasks.
#
# CERT_REQUIRED enforces verification against the system CA bundle. If this
# ever starts failing with SSLCertVerificationError, it means the CA bundle
# on this host is missing/stale (e.g. `sudo dnf install -y ca-certificates`),
# not that Upstash's cert is invalid.
if settings.REDIS_URL.startswith("rediss://"):
    _ssl_opts = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    celery_app.conf.broker_use_ssl = _ssl_opts
    celery_app.conf.redis_backend_use_ssl = _ssl_opts

import app.core.celery_tasks  # noqa: E402, F401

_LOG_FILE_NAME = "celery_worker.log"


@after_setup_logger.connect
def setup_celery_file_logging(logger: logging.Logger, **kwargs) -> None:
    root_logger = logging.getLogger()

    # Guard against duplicate handlers: after_setup_logger can fire more
    # than once per process (e.g. --autoreload, prefork worker restarts),
    # and without this check each firing adds another RotatingFileHandler,
    # duplicating every log line written after that point.
    already_attached = any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "") == str(settings.LOG_DIR / _LOG_FILE_NAME)
        for h in root_logger.handlers
    )
    if already_attached:
        return

    handler = RotatingFileHandler(
        settings.LOG_DIR / _LOG_FILE_NAME,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))

    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)