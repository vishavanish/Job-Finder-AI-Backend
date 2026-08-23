"""
app/core/celery_app.py
------------------------
The Celery application instance. Broker AND result backend both point at
your Upstash Redis database — Upstash speaks the standard Redis protocol
over TCP with TLS, so this is a completely normal redis:// URL, just
pointed at Upstash's host instead of a self-hosted Redis.

Upstash gives you a connection string that looks like:
    rediss://default:<password>@<region>-<name>.upstash.io:<port>
(note the double-s in "rediss" — that's TLS, required by Upstash's free
tier; a plain "redis://" URL will fail to connect.)

Run a worker with:
    celery -A app.core.celery_app worker --loglevel=info --pool=solo

--pool=solo is required on Windows (the default prefork pool uses
os.fork, which doesn't exist on Windows). On Linux/Mac in production you
can drop --pool=solo and let it use prefork with multiple worker
processes for real parallelism.
"""
from __future__ import annotations

import logging

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
    # Upstash free tier has a command budget (500K/month) — these settings
    # control how often the worker polls Redis, which drives idle command
    # usage. Defaults are reasonable; this is the knob if you need to trade
    # latency for lower Redis command usage.
    broker_transport_options={"visibility_timeout": 3600},
    worker_prefetch_multiplier=1,
)

# Explicit import (not autodiscover_tasks) so all @celery_app.task
# definitions in app/core/celery_tasks.py get registered. autodiscover_tasks
# only looks for a "tasks" module inside each *package* it's given
# (e.g. app.services.tasks) — it will NOT find app/core/celery_tasks.py,
# and will silently register zero tasks instead of raising an error. An
# explicit import fails loudly at worker startup if something's wrong,
# instead of letting every dispatched task sit PENDING forever.
import app.core.celery_tasks  # noqa: E402, F401


@after_setup_logger.connect
def setup_celery_file_logging(logger: logging.Logger, **kwargs) -> None:
    """Celery workers are separate OS processes from the FastAPI app, so
    they need their own log file handler — the RotatingFileHandler set up
    in app/main.py only applies to the API process, not worker processes."""
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        settings.LOG_DIR / "celery_worker.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
    logger.addHandler(handler)