"""
app/core/redis_client.py
---------------------------
One lazily-created redis-py client per process, on the same Redis that
Celery uses as broker/backend (settings.REDIS_URL). Shared by every
module that keeps ephemeral state in Redis (live pipeline progress,
task ownership, secret refs) so they reuse one connection pool. Tests
swap it for fakeredis via set_redis().
"""
from __future__ import annotations

import redis

from app.core.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    return _client


def set_redis(client: redis.Redis | None) -> None:
    global _client
    _client = client
