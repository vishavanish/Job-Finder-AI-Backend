from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import pending_response
from app.models.schemas import ScoreRequest, TaskResponse
from app.core.celery_tasks import score_task

router = APIRouter(prefix="/score", tags=["score"])
logger = logging.getLogger("job_finder_api.score")
settings = get_settings()


def _unwrap_secret(value) -> str:
    """SecretStr fields must be unwrapped to plain str here, at the last
    point before the payload is handed to Celery — Celery JSON-serializes
    task args, and SecretStr is not JSON-serializable. Everywhere upstream
    of this point (FastAPI validation, request logging, tracebacks) the
    value stays wrapped so it never gets printed/logged in the clear."""
    return value.get_secret_value() if value else ""


@router.post(
    "",
    response_model=TaskResponse,
    status_code=202,
    dependencies=[Depends(get_current_user)],
)
@limiter.limit(settings.RATE_LIMIT_SCORE)
async def start_score(request: Request, req: ScoreRequest):
    """Kicks off LLM-based job scoring (keyword pre-filter -> Gemini ->
    Qwen/HF fallback) on the Celery "default" queue. Poll
    GET /tasks/{task_id} for status and the ranked job list."""
    jobs = [j.model_dump() for j in req.jobs]
    score_params = {
        "resume_summary": req.resume_summary,
        "skills": req.skills,
        "career_targets": req.career_targets,
        "keyword_prefilter_min_pct": req.keyword_prefilter_min_pct,
        "llm_top_n_to_rank": req.llm_top_n_to_rank,
        "llm_min_score_to_keep": req.llm_min_score_to_keep,
        "gemini_model": req.gemini_model,
        "hf_model": req.hf_model,
        "gemini_api_key": _unwrap_secret(req.gemini_api_key),
        "hf_api_key": _unwrap_secret(req.hf_api_key),
    }

    async_result = score_task.apply_async(args=[jobs, score_params], queue=settings.default_queue)
    logger.info("[task=%s] dispatched to 'default' queue", async_result.id)
    return pending_response(async_result)