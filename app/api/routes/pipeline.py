from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import pending_response
from app.models.schemas import PipelineRequest, TaskResponse
from app.core.celery_tasks import pipeline_task

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
logger = logging.getLogger("job_finder_api.pipeline")
settings = get_settings()


def _unwrap_secret(value) -> str:
    return value.get_secret_value() if value else ""


@router.post(
    "/run",
    response_model=TaskResponse,
    status_code=202,
    dependencies=[Depends(get_current_user)],
)
@limiter.limit(settings.RATE_LIMIT_PIPELINE)
async def run_pipeline(request: Request, req: PipelineRequest, user=Depends(get_current_user)):
    """Runs search -> score -> (optional) apply as a single Celery task.
    Poll GET /tasks/{task_id} for status and the combined result."""
    search_params = {
        "keywords": req.search.keywords,
        "locations": req.search.locations,
        "sources": req.search.sources,
        "results_per_keyword": req.search.results_per_keyword,
        "max_age_hours": req.search.max_age_hours,
        "country_indeed": req.search.country_indeed,
        "linkedin_fetch_description": req.search.linkedin_fetch_description,
        "search_delay_sec": req.search.search_delay_sec,
        "linkedin_max_retries": req.search.linkedin_max_retries,
        "linkedin_retry_delay_sec": req.search.linkedin_retry_delay_sec,
        "enable_naukri_auto": req.search.enable_naukri_auto,
        "manual_naukri_jobs": [j.model_dump() for j in req.search.manual_naukri_jobs],
        "enable_naukri_apify": req.search.enable_naukri_apify,
        "apify_naukri_actor_id": req.search.apify_naukri_actor_id,
        "apify_api_token": _unwrap_secret(req.search.apify_api_token),
    }

    score_params = {
        "resume_summary": req.score.resume_summary,
        "skills": req.score.skills,
        "career_targets": req.score.career_targets,
        "keyword_prefilter_min_pct": req.score.keyword_prefilter_min_pct,
        "llm_top_n_to_rank": req.score.llm_top_n_to_rank,
        "llm_min_score_to_keep": req.score.llm_min_score_to_keep,
        "gemini_model": req.score.gemini_model,
        "hf_model": req.score.hf_model,
        "gemini_api_key": _unwrap_secret(req.score.gemini_api_key),
        "hf_api_key": _unwrap_secret(req.score.hf_api_key),
    }

    apply_params = None
    if req.apply:
        if req.apply.browser_profile_dir:
            logger.warning("ignored client-supplied browser_profile_dir override")
        apply_params = {
            "applicant_info": req.apply.applicant_info.model_dump(),
            "open_top_n": req.apply.open_top_n,
            "auto_fill_easy_apply": req.apply.auto_fill_easy_apply,
            "pause_between_tabs_sec": req.apply.pause_between_tabs_sec,
            "browser_profile_dir": None,
            "headless": req.apply.headless,
            "user_id": user.id,
            "require_auto_apply_capable": req.apply.require_auto_apply_capable,
        }

    async_result = pipeline_task.apply_async(
        args=[search_params, score_params, apply_params], queue="default"
    )
    logger.info("[task=%s] dispatched to 'default' queue for user=%s", async_result.id, user.id)
    return pending_response(async_result)
