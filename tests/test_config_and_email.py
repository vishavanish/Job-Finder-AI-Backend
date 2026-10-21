from datetime import datetime, timezone

from app.core.celery_app import celery_app
from app.core.config import _is_local_redis, get_settings
from app.providers.gmail import received_at as message_received_at


def test_local_redis_needs_no_tls():
    assert _is_local_redis("redis://localhost:6379/0")
    assert _is_local_redis("redis://:secret@127.0.0.1:6379/0")
    assert _is_local_redis("redis://[::1]:6379")
    assert not _is_local_redis("redis://default:x@eu1-able-cat.upstash.io:6379")


def test_worker_consumes_every_queue_the_api_publishes_to():
    settings = get_settings()
    queues = settings.worker_queues.split(",")
    assert settings.default_queue in queues
    assert settings.apply_queue in queues
    assert celery_app.conf.task_default_queue == settings.default_queue


def test_dev_queues_are_suffixed():
    assert get_settings().worker_queues == "default_dev,apply_dev,maintenance_dev,email_dev"


def test_email_uses_gmail_internal_date():
    received = message_received_at({"internalDate": "1760000000000"})
    assert received == datetime.fromtimestamp(1760000000, tz=timezone.utc)
