from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import pending_response
from app.models.schemas import SearchRequest, TaskResponse
from app.core.celery_tasks import search_task

router = APIRouter(prefix="/search", tags=["search"])
logger = logging.getLogger("job_finder_api.search")
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
@limiter.limit(settings.RATE_LIMIT_SEARCH)
async def start_search(request: Request, req: SearchRequest):
    """Kicks off a job search across LinkedIn/Indeed/Naukri on the Celery
    "default" queue (this can take minutes due to rate-limit pacing — any
    available "default"-queue worker process can pick it up). Poll
    GET /tasks/{task_id} for status and the resulting job list."""
    payload = {
        "keywords": req.keywords,
        "locations": req.locations,
        "sources": req.sources,
        "results_per_keyword": req.results_per_keyword,
        "max_age_hours": req.max_age_hours,
        "country_indeed": req.country_indeed,
        "linkedin_fetch_description": req.linkedin_fetch_description,
        "search_delay_sec": req.search_delay_sec,
        "linkedin_max_retries": req.linkedin_max_retries,
        "linkedin_retry_delay_sec": req.linkedin_retry_delay_sec,
        "enable_naukri_auto": req.enable_naukri_auto,
        "manual_naukri_jobs": [j.model_dump() for j in req.manual_naukri_jobs],
        "enable_naukri_apify": req.enable_naukri_apify,
        "apify_naukri_actor_id": req.apify_naukri_actor_id,
        "apify_api_token": _unwrap_secret(req.apify_api_token),
    }

    async_result = search_task.apply_async(args=[payload], queue="default")
    logger.info("[task=%s] dispatched to 'default' queue", async_result.id)
    return pending_response(async_result)