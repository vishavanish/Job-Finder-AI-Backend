from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.services.entitlements import check_feature, require_feature
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.schemas import PipelineRequest, TaskResponse
from app.core.celery_tasks import dispatch_pipeline_group, pipeline_task
from app.services import pipeline_progress
from app.services.task_params import build_apply_params, build_pipeline_params

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
logger = logging.getLogger("job_finder_api.pipeline")
settings = get_settings()


@router.post("/run/stream", status_code=202)
@limiter.limit(settings.RATE_LIMIT_PIPELINE)
async def run_pipeline_stream(
    request: Request,
    req: PipelineRequest,
    user=Depends(require_feature("job_search")),
    _scoring=Depends(require_feature("scoring")),
    db: Session = Depends(get_db),
):
    """Fan-out version: dispatches one search+score sub-task PER KEYWORD
    in parallel (Celery group), each pushing scored jobs to Redis the
    instant they're ready. Returns immediately with a run_id — poll
    GET /pipeline/{run_id}/jobs to watch results arrive live, and
    GET /pipeline/{run_id} for run-level progress (X/Y keywords done).

    Each job appears once per run. If a better copy of a job already in the
    feed turns up later (LinkedIn over Indeed), an updated entry with the
    SAME `id` and `replaces: true` is appended — upsert entries by `id`."""
    search_params, score_params = build_pipeline_params(req, db=db, user=user)
    keywords = search_params["keywords"]

    run_id = pipeline_progress.create_run(user_id=user.id, total_keywords=len(keywords))
    dispatch_pipeline_group.apply_async(args=[run_id, search_params, score_params], queue=settings.default_queue)
    logger.info("[run=%s] dispatched %d-keyword group for user=%s", run_id, len(keywords), user.id)
    return {"run_id": run_id, "keywords": keywords, "skipped_sources": search_params["skipped_sources"]}

@router.get("/{run_id}")
async def get_pipeline_run(run_id: str, user=Depends(get_current_user)):
    meta = pipeline_progress.get_run_meta(run_id=run_id)
    if not meta or meta["user_id"] != user.id:
        raise HTTPException(404, "no run found with this id")
    return meta


@router.get("/{run_id}/jobs")
async def get_pipeline_run_jobs(run_id: str, offset: int = 0, user=Depends(get_current_user)):
    """Poll this every ~2s, passing `offset` = the previous response's
    `next_offset`, to fetch only the NEW entries each tick. An entry whose
    `id` you already have (`replaces: true`) is a better copy of that job:
    replace it in place."""
    meta = pipeline_progress.get_run_meta(run_id=run_id)
    if not meta or meta["user_id"] != user.id:
        raise HTTPException(404, "no run found with this id")

    offset = max(offset, 0)
    jobs = pipeline_progress.get_jobs(run_id=run_id, offset=offset)
    return {"run_status": meta["status"], "jobs": jobs, "next_offset": offset + len(jobs)}
    
@router.post(
    "/run",
    response_model=TaskResponse,
    status_code=202,
)
@limiter.limit(settings.RATE_LIMIT_PIPELINE)
async def run_pipeline(
    request: Request,
    req: PipelineRequest,
    user=Depends(require_feature("job_search")),
    _scoring=Depends(require_feature("scoring")),
    db: Session = Depends(get_db),
):
    """Runs search -> score -> (optional) apply as a single Celery task.
    Poll GET /tasks/{task_id} for status and the combined result."""
    apply_params = None
    if req.apply:
        check_feature(db, user, "auto_apply")
        if req.apply.browser_profile_dir:
            logger.warning("ignored client-supplied browser_profile_dir override")
        apply_params = build_apply_params(req.apply, user_id=user.id)

    search_params, score_params = build_pipeline_params(req, db=db, user=user)
    response = dispatch(
        pipeline_task,
        args=[search_params, score_params, apply_params],
        queue=settings.default_queue,
        user_id=user.id,
    )
    logger.info("[task=%s] dispatched to '%s' for user=%s", response.task_id, settings.default_queue, user.id)
    return response
