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

from app.core.redis_client import get_redis

logger = logging.getLogger("job_finder_api.pipeline_progress")

RUN_TTL_SECONDS = 60 * 60 * 6  # 6h — matches TASK_RESULT_TTL_SECONDS' spirit

def _redis() -> redis.Redis:
    return get_redis()


def _jobs_key(run_id: str) -> str:
    return f"pipeline:{run_id}:jobs"


def _meta_key(run_id: str) -> str:
    return f"pipeline:{run_id}:meta"


def _keys_key(run_id: str) -> str:
    return f"pipeline:{run_id}:job_keys"


def _recorded_key(run_id: str) -> str:
    return f"pipeline:{run_id}:recorded"


def claim_job_key(*, run_id: str, job_key: str) -> bool:
    """SADD — True only for the FIRST caller to claim this job_key in the
    run. Keyword tasks call this before the LLM step, so a job that
    several keywords (or sources) surface is scored once, not N times.

    The claimer scores the canonical copy (job_store), and a better copy
    found later by another keyword task replaces the shown one through
    replace_recorded — so the first claimer no longer decides the source."""
    r = _redis()
    key = _keys_key(run_id)
    is_new = bool(r.sadd(key, job_key))
    r.expire(key, RUN_TTL_SECONDS)
    return is_new


def create_run(*, user_id: str, total_keywords: int, kind: str = "search", keywords: list[str] | None = None,
               skipped_sources: list[str] | None = None) -> str:
    """`kind` "search" = a search the user started on Jobs (GET /pipeline/runs/latest
    finds it again); "alert" = a job-alert run in the background."""
    run_id = str(uuid.uuid4())
    r = _redis()
    r.hset(_meta_key(run_id), mapping={
        "user_id": user_id,
        "status": "running",
        "total_keywords": total_keywords,
        "keywords_completed": 0,
        "error": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "seq": r.incr("pipeline_run_seq"),  # orders runs made in the same clock tick
        "keywords": json.dumps(keywords or []),
        "skipped_sources": json.dumps(skipped_sources or []),
    })
    r.expire(_meta_key(run_id), RUN_TTL_SECONDS)
    r.sadd(_user_runs_key(user_id), run_id)
    r.expire(_user_runs_key(user_id), RUN_TTL_SECONDS)
    return run_id


def _user_runs_key(user_id: str) -> str:
    return f"user_runs:{user_id}"


def latest_search_run(user_id: str) -> dict | None:
    """The user's newest search started from Jobs that Redis still holds
    (runs live RUN_TTL_SECONDS), with its keywords and skipped sources."""
    r = _redis()
    newest: tuple[tuple[str, int], str] | None = None
    for run_id in r.smembers(_user_runs_key(user_id)):
        meta = r.hgetall(_meta_key(run_id))
        if not meta or meta.get("kind", "search") != "search":
            continue
        order = (meta.get("created_at", ""), int(meta.get("seq") or 0))
        if newest is None or order > newest[0]:
            newest = (order, run_id)
    if newest is None:
        return None
    run = get_run_meta(run_id=newest[1])
    if run is None:
        return None
    meta = r.hgetall(_meta_key(newest[1]))
    run.update(created_at=newest[0][0], keywords=json.loads(meta.get("keywords") or "[]"),
               skipped_sources=json.loads(meta.get("skipped_sources") or "[]"))
    return run


def cancel_runs_for_user(user_id: str) -> int:
    """Marks every live run of this user 'cancelled'; keyword tasks that
    haven't started yet check is_cancelled() and skip their work."""
    r = _redis()
    cancelled = 0
    for run_id in r.smembers(_user_runs_key(user_id)):
        if r.exists(_meta_key(run_id)):
            mark_run_status(run_id=run_id, status="cancelled", error="cancelled: account disabled")
            cancelled += 1
    return cancelled


def is_cancelled(run_id: str) -> bool:
    return _redis().hget(_meta_key(run_id), "status") == "cancelled"


def record_scored_job(*, run_id: str, job: dict) -> dict:
    """RPUSH — append-only, safe under concurrent callers from multiple
    group() workers with zero coordination needed; Redis serializes
    concurrent RPUSH calls on the same key natively.

    `id` is the canonical job id when the job came from job_store, so the
    feed, save/hide and later replacements all use the same id."""
    payload = {
        "id": job.get("id") or str(uuid.uuid4()),
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
        # P4a: the company's own apply page and which apply buttons work
        "apply_url": job.get("apply_url"),
        "apply_options": _apply_options(job),
        # P3.2: match breakdown and freshness (None when unknown)
        "match": job.get("match"),
        "posted_at": job.get("posted_at"),
        "posted_hours_ago": job.get("posted_hours_ago"),
        "early": bool(job.get("early")),
        "new_since_last_search": job.get("new_since_last_search"),
        "applicants": job.get("applicants"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _push(run_id, payload)
    return payload


def _apply_options(job: dict) -> dict:
    from app.services import apply_options
    return apply_options.options(job)


def _push(run_id: str, payload: dict) -> None:
    r = _redis()
    key = _jobs_key(run_id)
    r.rpush(key, json.dumps(payload))
    r.expire(key, RUN_TTL_SECONDS)  # refresh TTL — an active run never expires mid-flight


def remember_recorded(*, run_id: str, job_key: str, payload: dict) -> None:
    """Which feed entry a job_key became, so a better copy can replace it."""
    r = _redis()
    r.hset(_recorded_key(run_id), job_key, json.dumps(payload))
    r.expire(_recorded_key(run_id), RUN_TTL_SECONDS)


def replace_recorded(*, run_id: str, job_key: str, best: dict) -> bool:
    """If this job is already in the feed from a different copy than
    `best` (the canonical copy), appends an updated entry with the SAME id
    and `replaces: true`. Clients upsert feed entries by `id`. Scores are
    kept: it is the same job. Returns True if an update was pushed."""
    raw = _redis().hget(_recorded_key(run_id), job_key)
    if not raw:
        return False
    shown = json.loads(raw)
    if shown.get("source") == best.get("source") and shown.get("url") == best.get("url"):
        return False
    updated = {**shown, **{k: best.get(k, shown.get(k)) for k in
                           ("source", "title", "company", "location", "url", "description", "posted")},
               "auto_apply_capable": bool(best.get("auto_apply_capable")), "replaces": True,
               "apply_url": best.get("apply_url"), "apply_options": _apply_options(best),
               "created_at": datetime.now(timezone.utc).isoformat()}
    _push(run_id, updated)
    remember_recorded(run_id=run_id, job_key=job_key, payload=updated)
    return True


def update_recorded(*, run_id: str, job_key: str, fields: dict) -> bool:
    """Appends an updated copy of a job already in the feed with some
    fields changed (e.g. the LinkedIn applicant count found after it was
    shown) — same `id`, `replaces: true`."""
    raw = _redis().hget(_recorded_key(run_id), job_key)
    if not raw:
        return False
    shown = json.loads(raw)
    if all(shown.get(k) == v for k, v in fields.items()):
        return False
    updated = {**shown, **fields, "replaces": True, "created_at": datetime.now(timezone.utc).isoformat()}
    _push(run_id, updated)
    remember_recorded(run_id=run_id, job_key=job_key, payload=updated)
    return True


LAST_SEARCH_TTL_SECONDS = 90 * 24 * 3600


def mark_user_search(user_id: str) -> str | None:
    """Records that the user started a search now; returns when their
    previous one started (ISO), for "new since your last search"."""
    r = _redis()
    key = f"user_last_search:{user_id}"
    previous = r.getset(key, datetime.now(timezone.utc).isoformat())
    r.expire(key, LAST_SEARCH_TTL_SECONDS)
    return previous


def count_jobs(*, run_id: str) -> int:
    return int(_redis().llen(_jobs_key(run_id)))


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