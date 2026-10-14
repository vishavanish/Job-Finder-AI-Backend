from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.schemas import ApplyRequest, TaskResponse
from app.core.celery_tasks import apply_task
from app.services.task_params import build_apply_params

router = APIRouter(prefix="/apply", tags=["apply"])
logger = logging.getLogger("job_finder_api.apply")
settings = get_settings()


@router.post("", response_model=TaskResponse, status_code=202)
@limiter.limit(settings.RATE_LIMIT_APPLY)
async def start_apply(request: Request, req: ApplyRequest, user=Depends(get_current_user)):
    """Opens top-matched jobs in a real Playwright browser on the Celery
    worker and pre-fills LinkedIn Easy Apply forms — never auto-submits.
    Poll GET /tasks/{task_id} for status, and GET /applications for the
    tracked application rows (scoped to your account).

    NOTE: a client-supplied browser_profile_dir is ignored — see
    build_apply_params.
    """
    if req.browser_profile_dir:
        logger.warning("ignored client-supplied browser_profile_dir override")
    jobs = [j.model_dump() for j in req.jobs]
    response = dispatch(
        apply_task, args=[jobs, build_apply_params(req, user_id=user.id)],
        queue=settings.apply_queue, user_id=user.id,
    )
    logger.info("[task=%s] dispatched to '%s' for user=%s", response.task_id, settings.apply_queue, user.id)
    return response
