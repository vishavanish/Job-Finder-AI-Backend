"""
app/services/pipeline_progress.py
------------------------------------
Redis-backed (not Postgres) live progress store for the streaming
pipeline. Uses the SAME Redis instance Celery already uses as its
broker/backend (settings.REDIS_URL) — no new database table, no
migration.

WHY REDIS DIRECTLY, NOT CELERY'S RESULT-BACKEND API: Celery's own
result backend stores one blob per task_id — fine for "this one task
finished, here's its output," but group() gives you N independent
task_ids with N independent blobs. There's no Celery-level primitive
for "N tasks append to one shared, growing list." So this module talks
to Redis with a plain redis-py client instead, using two structures per
run:

  - a Redis LIST  "pipeline:{run_id}:jobs"   — RPUSH one JSON blob per
    scored job, in arrival order. LRANGE reads a slice. Since jobs are
    only ever appended (never mutated), reading "give me everything
    after index N" is just LRANGE(N, -1) — no need to track timestamps
    for pagination the way the DB version needed `since`.
  - a Redis HASH  "pipeline:{run_id}:meta"   — status, total_keywords,
    keywords_completed, error. keywords_completed is incremented with
    HINCRBY, which is atomic at the Redis level — safe under concurrent
    group() workers with no extra locking, same guarantee the DB
    version got from SQL UPDATE ... SET x = x + 1.

TTL: every key for a run is set to expire (RUN_TTL_SECONDS) so
abandoned/old runs don't accumulate in Redis forever. Extend the TTL
on every write so an active run never expires mid-flight.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import redis

from app.core.config import get_settings

logger = logging.getLogger("job_finder_api.pipeline_progress")

RUN_TTL_SECONDS = 60 * 60 * 6  # 6h — matches TASK_RESULT_TTL_SECONDS' spirit

_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    """Lazy singleton — module-level so every call in this process reuses
    one connection pool instead of opening a new client per call."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(settings.REDIS_URL, decode_responses=True, ssl_cert_reqs=None)
    return _client


def _jobs_key(run_id: str) -> str:
    return f"pipeline:{run_id}:jobs"


def _meta_key(run_id: str) -> str:
    return f"pipeline:{run_id}:meta"


def create_run(*, user_id: str, total_keywords: int) -> str:
    run_id = str(uuid.uuid4())
    r = _redis()
    r.hset(_meta_key(run_id), mapping={
        "user_id": user_id,
        "status": "running",
        "total_keywords": total_keywords,
        "keywords_completed": 0,
        "error": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    r.expire(_meta_key(run_id), RUN_TTL_SECONDS)
    return run_id


def record_scored_job(*, run_id: str, job: dict) -> None:
    """RPUSH — append-only, safe under concurrent callers from multiple
    group() workers with zero coordination needed; Redis serializes
    concurrent RPUSH calls on the same key natively."""
    r = _redis()
    payload = {
        "id": str(uuid.uuid4()),
        "source": job.get("source", ""),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "description": job.get("description", ""),
        "posted": job.get("posted", ""),
        "keyword_score": job.get("keyword_score"),
        "llm_score": job.get("llm_score"),
        "llm_reason": job.get("llm_reason", ""),
        "auto_apply_capable": bool(job.get("auto_apply_capable")),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    key = _jobs_key(run_id)
    r.rpush(key, json.dumps(payload))
    r.expire(key, RUN_TTL_SECONDS)  # refresh TTL — an active run never expires mid-flight


def get_jobs(*, run_id: str, offset: int = 0) -> list[dict]:
    """Returns jobs from `offset` onward, in the order they were scored.
    Frontend passes back the count it already has as `offset` on the
    next poll, so it only receives NEW jobs each tick — same effect as
    the DB version's `since` timestamp filter, simpler to implement
    against a Redis list."""
    r = _redis()
    raw = r.lrange(_jobs_key(run_id), offset, -1)
    return [json.loads(item) for item in raw]


def increment_keyword_completed(*, run_id: str) -> None:
    """HINCRBY — atomic at the Redis level. This is the Redis-native
    equivalent of the earlier SQL `UPDATE ... SET x = x + 1`: the
    increment happens inside Redis itself, so concurrent group()
    workers calling this simultaneously can't lose an increment the way
    a naive read-then-write in Python could."""
    r = _redis()
    r.hincrby(_meta_key(run_id), "keywords_completed", 1)
    r.expire(_meta_key(run_id), RUN_TTL_SECONDS)


def mark_run_status(*, run_id: str, status: str, error: str | None = None) -> None:
    r = _redis()
    r.hset(_meta_key(run_id), mapping={"status": status, "error": error or ""})


def get_run_meta(*, run_id: str) -> dict | None:
    """Derives 'completed' from keywords_completed >= total_keywords
    rather than relying on a chord callback to explicitly set status —
    see dispatch_pipeline_group's docstring for why the callback isn't
    used. This function is now the single source of truth for whether
    a run is done."""
    r = _redis()
    meta = r.hgetall(_meta_key(run_id))
    if not meta:
        return None

    total = int(meta.get("total_keywords", 0))
    completed = int(meta.get("keywords_completed", 0))
    status = meta.get("status", "pending")

    if status == "running" and total > 0 and completed >= total:
        status = "completed"

    return {
        "run_id": run_id,
        "user_id": meta.get("user_id", ""),
        "status": status,
        "total_keywords": total,
        "keywords_completed": completed,
        "error": meta.get("error") or None,
    }