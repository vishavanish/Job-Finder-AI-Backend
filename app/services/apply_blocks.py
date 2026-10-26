"""
app/services/apply_blocks.py
------------------------------
Platforms that refuse the server's apply browser (QA5). Every auto run
comes from the same server IP, so when SmartRecruiters (say) shows a bot /
access block page, Auto apply on that platform is paused for everyone:
  - APPLY_BLOCK_PAUSE_HOURS (24) the first time; a new block within a week
    of the last one doubles the pause, up to APPLY_BLOCK_MAX_PAUSE_HOURS;
  - apply_options shows Auto apply disabled with the reason, create_run
    refuses it (409 platform_blocked);
  - after the pause the next click tries again; a page that opens cleanly
    forgets the old block (note_ok); an admin can clear a pause.
Blocks are never worked around (no proxies, no disguised browsers).

State: Redis hash `apply_blocked`, field = platform key, value = JSON
{name, until, hours, at, run_id}. Read once per CACHE_SECONDS per process
(the feed asks for many jobs at once).
"""
from __future__ import annotations

import json
import math
import time

from app.apply.sites import GENERIC_HOSTS, host_of, site_of
from app.core.config import get_settings
from app.core.redis_client import get_redis

HASH = "apply_blocked"
CACHE_SECONDS = 30
REPEAT_WINDOW_SECONDS = 7 * 24 * 3600
_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "linkedin": "LinkedIn"}
_SOURCE_NAMES = {name.lower(): name for name in GENERIC_HOSTS.values()}

_cache: tuple[float, int, dict[str, dict]] | None = None  # (read at, id of the Redis client, rows)


def platform_of(adapter: str | None, url: str | None, source: str | None = None) -> tuple[str, str]:
    """(key, display name) of the site a run's browser talks to."""
    if adapter in _NAMES:
        return adapter, _NAMES[adapter]
    host = host_of(url or "")
    for domain, name in GENERIC_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return name.lower(), name
    name = _SOURCE_NAMES.get((source or "").lower())
    if name:
        return name.lower(), name
    site = site_of(host) if host else "unknown"
    return site, site


def _read() -> dict[str, dict]:
    global _cache
    now = time.time()
    r = get_redis()
    if _cache is not None and now - _cache[0] < CACHE_SECONDS and _cache[1] == id(r):
        return _cache[2]
    rows: dict[str, dict] = {}
    for key, raw in (r.hgetall(HASH) or {}).items():
        try:
            rows[key] = json.loads(raw)
        except ValueError:
            continue
    _cache = (now, id(r), rows)
    return rows


def _forget_cache() -> None:
    global _cache
    _cache = None


def paused(key: str) -> dict | None:
    """The pause on this platform while it lasts: {name, until, hours, at, run_id}."""
    row = _read().get(key)
    return row if row and row.get("until", 0) > time.time() else None


def active() -> list[dict]:
    """Every platform paused now, for admin health."""
    now = time.time()
    return sorted(({"platform": k, **v} for k, v in _read().items() if v.get("until", 0) > now),
                  key=lambda r: r["until"])


def record(key: str, name: str, run_id: str | None = None) -> dict:
    """Pauses the platform after a block; a repeat within a week doubles the pause."""
    cfg = get_settings()
    r = get_redis()
    now = time.time()
    hours = cfg.APPLY_BLOCK_PAUSE_HOURS
    raw = r.hget(HASH, key)
    if raw:
        try:
            prev = json.loads(raw)
            if now - prev.get("at", 0) < REPEAT_WINDOW_SECONDS:
                hours = prev.get("hours", hours) * 2
        except ValueError:
            pass
    hours = min(hours, cfg.APPLY_BLOCK_MAX_PAUSE_HOURS)
    row = {"name": name, "until": now + hours * 3600, "hours": hours, "at": now, "run_id": run_id}
    r.hset(HASH, key, json.dumps(row))
    _forget_cache()
    return row


def note_ok(key: str) -> None:
    """The platform's page opened cleanly: an expired block no longer counts as a repeat."""
    row = _read().get(key)
    if row and row.get("until", 0) <= time.time():
        get_redis().hdel(HASH, key)
        _forget_cache()


def clear(key: str) -> dict | None:
    """Admin: ends a pause now. Returns what was removed."""
    r = get_redis()
    raw = r.hget(HASH, key)
    r.hdel(HASH, key)
    _forget_cache()
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def hours_left(row: dict) -> int:
    return max(1, math.ceil((row["until"] - time.time()) / 3600))


def reason(row: dict) -> str:
    left = hours_left(row)
    when = "within the hour" if left <= 1 else f"in about {left} h"
    return (f"{row['name']} is blocking automated applications from our server right now. Apply manually — "
            f"Auto apply is tried again {when}.")
