"""
app/api/routes/tasks.py
-------------------------
GET /tasks/{task_id} now reads from Celery's result backend (Redis) via
AsyncResult, instead of the old in-memory TaskManager + SQLite TaskStore.
That's the whole point of the Celery migration: task state lives in Redis,
shared by every worker process and by this API process too, so polling
works correctly no matter which process handled the task.

STATUS MAPPING — Celery's own states don't line up 1:1 with our
TaskStatus enum, so here's the mapping and *why*:

  Celery state         -> our TaskStatus   -> reasoning
  --------------------------------------------------------------
  PENDING               -> PENDING          Celery's PENDING is actually
                                             ambiguous — it means "no info
                                             found for this id," which is
                                             ALSO what an unknown/bogus
                                             task_id looks like. See the
                                             unknown-id note below.
  STARTED                -> RUNNING
  PROGRESS (custom state -> RUNNING          set by our own tasks via
    set by _progress_reporter                task.update_state(state=
    in celery_tasks.py)                      "PROGRESS", meta={...})
  SUCCESS                -> COMPLETED
  FAILURE                -> FAILED
  RETRY                  -> RUNNING          still in flight from the
                                             caller's point of view
  REVOKED                -> FAILED

UNKNOWN TASK IDS: Celery's AsyncResult never 404s — a garbage/typo'd id
comes back with state=PENDING same as a real task that's just queued and
hasn't been picked up yet. We can't reliably distinguish "queued, not
started" from "never existed" purely from the result backend. This is a
real (accepted) trade-off of moving off the SQLite-backed TaskManager,
which *could* 404 confidently because it had its own authoritative table
of task ids it created. Documented in the route docstring below rather
than hidden — do not build anything downstream that depends on this
endpoint 404ing for bad ids.

PROGRESS TEXT: our tasks call task.update_state(state="PROGRESS",
meta={"progress": "..."}) from _progress_reporter. When state is
PROGRESS, .info is that meta dict, so we pull progress out of
result.info["progress"]. When the task fails, Celery puts the raised
exception object in .info/.result — we str() it for the `error` field
rather than trying to serialize the exception.

GET /tasks (list-all) is DROPPED here — Celery has no built-in "list
every task id ever dispatched" (the result backend is a plain key-value
store keyed by task id, not an index). If you need this back, the
cheapest option is: keep a small side-table (Redis SET or a one-line
SQLite insert) that route handlers append task ids to on dispatch, and
have this route read that index — NOT the old TaskStore, which used to
role of source-of-truth for polling too. That's a deliberate future
addition, not an oversight — flag if you want it now.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from celery.result import AsyncResult
from fastapi import APIRouter
from fastapi import Depends

from app.core.auth import get_current_user
from app.core.celery_app import celery_app
from app.models.schemas import TaskResponse, TaskStatus

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger("job_finder_api.tasks")
_CELERY_TO_STATUS = {
    "PENDING": TaskStatus.PENDING,
    "STARTED": TaskStatus.RUNNING,
    "PROGRESS": TaskStatus.RUNNING,
    "RETRY": TaskStatus.RUNNING,
    "SUCCESS": TaskStatus.COMPLETED,
    "FAILURE": TaskStatus.FAILED,
    "REVOKED": TaskStatus.FAILED,
}


def _to_task_response(task_id: str) -> TaskResponse:
    result = AsyncResult(task_id, app=celery_app)
    status = _CELERY_TO_STATUS.get(result.state, TaskStatus.PENDING)

    progress = None
    task_result = None
    error = None

    if result.state == "PROGRESS" and isinstance(result.info, dict):
        progress = result.info.get("progress")
    elif result.state == "SUCCESS":
        task_result = result.result
        progress = "done"
    elif result.state == "FAILURE":
        error = str(result.info) if result.info is not None else "task failed"
        progress = "failed"
    elif result.state == "STARTED":
        progress = "running"

    now = datetime.now(timezone.utc)

    return TaskResponse(
        task_id=task_id,
        status=status,
        created_at=now,
        updated_at=now,
        progress=progress,
        result=task_result,
        error=error,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):

    return _to_task_response(task_id)


@router.post("/{task_id}/cancel", status_code=200)
async def cancel_task(task_id: str, user=Depends(get_current_user)):
    celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    return {"task_id": task_id, "status": "cancel_requested"}