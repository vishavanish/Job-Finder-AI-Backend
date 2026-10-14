from datetime import datetime, timezone

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.services.email_tracker import message_received_at


def test_worker_consumes_every_queue_the_api_publishes_to():
    settings = get_settings()
    queues = settings.worker_queues.split(",")
    assert settings.default_queue in queues
    assert settings.apply_queue in queues
    assert celery_app.conf.task_default_queue == settings.default_queue


def test_dev_queues_are_suffixed():
    assert get_settings().worker_queues == "default_dev,apply_dev"


def test_email_uses_gmail_internal_date():
    received = message_received_at({"internalDate": "1760000000000"})
    assert received == datetime.fromtimestamp(1760000000, tz=timezone.utc)
