from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.services.entitlements import require_feature
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import dispatch
from app.models.schemas import ScoreRequest, TaskResponse
from app.core.celery_tasks import score_task
from app.services.task_params import build_score_params

router = APIRouter(prefix="/score", tags=["score"])
logger = logging.getLogger("job_finder_api.score")
settings = get_settings()


@router.post("", response_model=TaskResponse, status_code=202)
@limiter.limit(settings.RATE_LIMIT_SCORE)
async def start_score(request: Request, req: ScoreRequest, user=Depends(require_feature("scoring"))):
    """Kicks off LLM-based job scoring (keyword pre-filter -> Gemini ->
    Groq -> HF fallback) on the Celery "default" queue. Poll
    GET /tasks/{task_id} for status and the ranked job list."""
    jobs = [j.model_dump() for j in req.jobs]
    response = dispatch(
        score_task, args=[jobs, build_score_params(req)], queue=settings.default_queue, user_id=user.id,
    )
    logger.info("[task=%s] dispatched to '%s' for user=%s", response.task_id, settings.default_queue, user.id)
    return response
