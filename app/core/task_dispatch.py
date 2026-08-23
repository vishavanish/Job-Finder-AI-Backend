"""
app/core/task_dispatch.py
----------------------------
Tiny shared helper so every route that dispatches a Celery task returns
the same TaskResponse shape without repeating the same four lines.
Deliberately does NOT touch the result backend — right after dispatch
the task is, by definition, PENDING, so there's no need to call
AsyncResult here; that happens on the first GET /tasks/{id} poll.
"""
from __future__ import annotations

from datetime import datetime, timezone

from celery.result import AsyncResult

from app.models.schemas import TaskResponse, TaskStatus


def pending_response(async_result: AsyncResult) -> TaskResponse:
    now = datetime.now(timezone.utc)
    return TaskResponse(
        task_id=async_result.id,
        status=TaskStatus.PENDING,
        created_at=now,
        updated_at=now,
        progress=None,
    )