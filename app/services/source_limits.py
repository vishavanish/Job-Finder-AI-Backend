"""
app/services/source_limits.py
--------------------------------
Redis-backed politeness for free job sources (CLAUDE.md 7.4):

  - cached_feed(): one download per feed per FEED_CACHE_TTL_SECONDS, shared
    by every keyword task and every user. A lock stops parallel tasks from
    all fetching at once; a short "failed" marker stops them all retrying a
    feed that just failed (only the task that fetched records the failure).
  - take_quota(): daily call caps for keyed APIs, per user, so one user
    cannot spend the platform's free quota. Own keys get a higher cap.
"""
from __future__ import annotations

import base64
import json
import time
import zlib
from datetime import datetime, timezone
from typing import Callable

from app.core.config import get_settings
from app.core.redis_client import get_redis

FEED_MAX_JOBS = 600
_FAIL_MARKER_SECONDS = 120
_WAIT_POLL_SECONDS = 0.5


class FeedUnavailable(Exception):
    """Another task is fetching this feed or it just failed; skip it this time."""


def _encode(jobs: list[dict]) -> str:
    return base64.b64encode(zlib.compress(json.dumps(jobs).encode("utf-8"))).decode("ascii")


def _decode(blob: str) -> list[dict]:
    return json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))


def cached_feed(key: str, fetch: Callable[[], list[dict]], *, wait_seconds: float = 20,
                ttl: int | None = None) -> list[dict]:
    r = get_redis()
    settings = get_settings()
    cache_key, lock_key, fail_key = f"feedcache:{key}", f"feedlock:{key}", f"feedfail:{key}"

    blob = r.get(cache_key)
    if blob:
        return _decode(blob)
    if r.exists(fail_key):
        raise FeedUnavailable(f"{key}: failed moments ago, will retry later")

    lock_ttl = settings.SOURCE_HTTP_TIMEOUT_SECONDS * 4 + 10
    if r.set(lock_key, "1", nx=True, ex=lock_ttl):
        try:
            jobs = fetch()[:FEED_MAX_JOBS]
        except Exception:
            r.set(fail_key, "1", ex=_FAIL_MARKER_SECONDS)
            raise
        finally:
            r.delete(lock_key)
        r.set(cache_key, _encode(jobs), ex=ttl or settings.FEED_CACHE_TTL_SECONDS)
        return jobs

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(_WAIT_POLL_SECONDS)
        blob = r.get(cache_key)
        if blob:
            return _decode(blob)
        if not r.exists(lock_key):
            break
    raise FeedUnavailable(f"{key}: another search is still downloading it")


def _quota_keys(source_key: str, user_id: str | None, own: bool) -> list[str]:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    keys = [f"srcquota:{source_key}:{user_id or 'anon'}:{day}"]
    return keys if own else keys + [f"srcquota:{source_key}:platform:{day}"]


def _bump(key: str) -> int:
    r = get_redis()
    n = r.incr(key)
    if n == 1:
        r.expire(key, 2 * 24 * 3600)
    return n


def take_quota(source_key: str, user_id: str | None, key_source: str) -> bool:
    """Counts one call; False when this user (or the shared platform key)
    is over today's cap. `key_source` is key_store.ResolvedKey.source."""
    settings = get_settings()
    own = key_source == "byok"
    cap = settings.SOURCE_DAILY_CAP_OWN_KEY if own else settings.SOURCE_DAILY_CAP_PLATFORM_KEY
    keys = _quota_keys(source_key, user_id, own)
    if _bump(keys[0]) > cap:
        return False
    if not own and _bump(keys[1]) > settings.SOURCE_DAILY_CAP_PLATFORM_TOTAL:
        return False
    return True


def refund_quota(source_key: str, user_id: str | None, key_source: str) -> None:
    """Gives a call back when it failed: a bad key or a refused request brought no jobs, so it must not use up
    the day's searches. Repeated failures are limited by the source breaker instead."""
    r = get_redis()
    for key in _quota_keys(source_key, user_id, key_source == "byok"):
        r.decr(key)
