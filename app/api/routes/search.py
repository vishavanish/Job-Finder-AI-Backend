from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.services.entitlements import require_feature
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.schemas import SearchRequest, TaskResponse
from app.core.celery_tasks import search_task
from app.services.task_params import build_search_params

router = APIRouter(prefix="/search", tags=["search"])
logger = logging.getLogger("job_finder_api.search")
settings = get_settings()


@router.post("", response_model=TaskResponse, status_code=202)
@limiter.limit(settings.RATE_LIMIT_SEARCH)
async def start_search(request: Request, req: SearchRequest, user=Depends(require_feature("job_search"))):
    """Kicks off a job search across LinkedIn/Indeed/Naukri on the Celery
    "default" queue (this can take minutes due to rate-limit pacing — any
    available "default"-queue worker process can pick it up). Poll
    GET /tasks/{task_id} for status and the resulting job list."""
    response = dispatch(
        search_task, args=[build_search_params(req)], queue=settings.default_queue, user_id=user.id,
    )
    logger.info("[task=%s] dispatched to '%s' for user=%s", response.task_id, settings.default_queue, user.id)
    return response
