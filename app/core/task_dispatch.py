"""
app/core/task_dispatch.py
----------------------------
Single dispatch path for every route that starts a Celery task. It
records task_id -> user_id in Redis BEFORE publishing, so
GET /tasks/{id} and POST /tasks/{id}/cancel can enforce ownership and
return 404 for ids the caller never created (Celery's result backend
alone can't tell "queued" from "never existed").

Deliberately does NOT touch the result backend — right after dispatch
the task is, by definition, PENDING, so there's no need to call
AsyncResult here; that happens on the first GET /tasks/{id} poll.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from celery import Task

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.schemas import TaskResponse, TaskStatus


def _owner_key(task_id: str) -> str:
    return f"task_owner:{task_id}"


def record_owner(task_id: str, user_id: str) -> None:
    get_redis().set(_owner_key(task_id), user_id, ex=get_settings().TASK_RESULT_TTL_SECONDS)


def owner_of(task_id: str) -> str | None:
    return get_redis().get(_owner_key(task_id))


def dispatch(task: Task, *, args: list, queue: str, user_id: str) -> TaskResponse:
    task_id = str(uuid.uuid4())
    record_owner(task_id, user_id)
    task.apply_async(args=args, queue=queue, task_id=task_id)

    now = datetime.now(timezone.utc)
    return TaskResponse(
        task_id=task_id,
        status=TaskStatus.PENDING,
        created_at=now,
        updated_at=now,
        progress=None,
    )
