"""
app/core/tasks.py
------------------------
Celery task definitions. This replaces the old pattern where routes
created an asyncio task and passed a `progress_cb` that used
`asyncio.run_coroutine_threadsafe` to report back to the FastAPI event
loop. That pattern ONLY works when the background work runs in the same
process as the API server (a thread in a ThreadPoolExecutor).

Celery workers are separate OS processes — sometimes on a different
machine entirely. There is no shared event loop to call back into. So
progress now goes through Celery's own mechanism: `self.update_state(...)`,
which writes to Redis (the result backend) and can be polled the same way
you polled the old in-memory task manager, just via
`AsyncResult(task_id).info` instead of a custom dict.

Each function below is decorated with @celery_app.task(bind=True) so it
receives `self`, giving access to self.update_state() for progress and
self.request.id for the task's own ID (useful for logging).
"""
from __future__ import annotations

import logging

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.services import browser_apply, company_ats_sources, job_sources, scorer

logger = logging.getLogger("job_finder_api.tasks")


import re

_SECRET_KEYS = ("api_key", "token", "password")

def _sanitize_progress_message(message: str) -> str:
    # collapse anything that looks like it's echoing a key/token value
    return re.sub(r'(key|token)[\s:=]+[\w\-\.]{8,}', r'\1=***REDACTED***', message, flags=re.IGNORECASE)

def _make_progress_cb(task, task_name: str):
    def progress_cb(message: str) -> None:
        safe_message = _sanitize_progress_message(message)
        logger.info("[task=%s] %s", task.request.id, safe_message)
        task.update_state(state="PROGRESS", meta={"progress": safe_message})
    return progress_cb


@celery_app.task(bind=True, name="job_finder.search")
def search_task(self, search_params: dict) -> dict:
    progress_cb = _make_progress_cb(self, "search")
    
    jobs = job_sources.scrape_all(
        keywords=search_params["keywords"],
        locations=search_params["locations"],
        sources=search_params["sources"],
        results_per_keyword=search_params.get("results_per_keyword", 25),
        max_age_hours=search_params.get("max_age_hours", 24),
        country_indeed=search_params.get("country_indeed", "India"),
        linkedin_fetch_description=search_params.get("linkedin_fetch_description", True),
        search_delay_sec=search_params.get("search_delay_sec", 15),
        linkedin_max_retries=search_params.get("linkedin_max_retries", 2),
        linkedin_retry_delay_sec=search_params.get("linkedin_retry_delay_sec", 30),
        enable_naukri_auto=search_params.get("enable_naukri_auto", False),
        manual_naukri_jobs=search_params.get("manual_naukri_jobs", []),
        enable_naukri_apify=search_params.get("enable_naukri_apify", False),
        apify_naukri_actor_id=search_params.get("apify_naukri_actor_id", ""),
        apify_api_token=search_params.get("apify_api_token") or "",
        progress=progress_cb,
    )
    return {"jobs": jobs, "total_raw": len(jobs), "total_usable": len(jobs)}


@celery_app.task(bind=True, name="job_finder.score")
def score_task(self, jobs: list[dict], score_params: dict) -> dict:
    settings = get_settings()
    progress_cb = _make_progress_cb(self, "score")
    return scorer.score_jobs(
        jobs,
        resume_summary=score_params["resume_summary"],
        skills=score_params["skills"],
        career_targets=score_params.get("career_targets", []),
        keyword_prefilter_min_pct=score_params.get("keyword_prefilter_min_pct", 30),
        llm_top_n_to_rank=score_params.get("llm_top_n_to_rank", 40),
        llm_min_score_to_keep=score_params.get("llm_min_score_to_keep", 70),
        gemini_model=score_params.get("gemini_model", "gemini-2.5-flash"),
        hf_model=score_params.get("hf_model", "Qwen/Qwen3-8B"),
        gemini_api_key=score_params.get("gemini_api_key") or settings.GEMINI_API_KEY,
        hf_api_key=score_params.get("hf_api_key") or settings.HF_API_KEY,
        progress=progress_cb,
    )


@celery_app.task(bind=True, name="job_finder.company_ats")
def company_ats_task(self, targets: list[dict], request_delay_sec: float) -> dict:
    progress_cb = _make_progress_cb(self, "company_ats")
    jobs = company_ats_sources.scrape_company_pages(
        targets, request_delay_sec=request_delay_sec, progress=progress_cb,
    )
    return {"jobs": jobs, "total": len(jobs)}


@celery_app.task(bind=True, name="job_finder.apply")
def apply_task(self, jobs: list[dict], apply_params: dict) -> dict:
    """IMPORTANT: this launches a real Playwright browser owned by the
    Celery WORKER process. See app/core/display.py's resolve_headless()."""
    from app.core.display import resolve_headless

    settings = get_settings()
    progress_cb = _make_progress_cb(self, "apply")

    actual_headless, warning = resolve_headless(apply_params.get("headless", False))
    if warning:
        progress_cb(warning)

    result = browser_apply.open_and_prepare(
        jobs,
        applicant_info=apply_params["applicant_info"],
        open_top_n=apply_params.get("open_top_n", 10),
        auto_fill_easy_apply=apply_params.get("auto_fill_easy_apply", True),
        pause_between_tabs_sec=apply_params.get("pause_between_tabs_sec", 2),
        browser_profile_dir=apply_params.get("browser_profile_dir") or str(settings.BROWSER_PROFILE_DIR),
        applications_log_path=settings.OUTPUT_DIR / "applications_log.csv",
        user_id=apply_params["user_id"],
        headless=actual_headless,
        require_auto_apply_capable=apply_params.get("require_auto_apply_capable", True),
        progress=progress_cb,
    )
    result.pop("handle", None)
    result["headless_used"] = actual_headless
    result["headless_override_warning"] = warning
    return result

@celery_app.task(bind=True, name="job_finder.pipeline")
def pipeline_task(self, search_params: dict, score_params: dict, apply_params: dict | None) -> dict:
    progress_cb = _make_progress_cb(self, "pipeline")
    settings = get_settings()

    progress_cb("step 1/3: scraping")
    jobs = job_sources.scrape_all(
        keywords=search_params["keywords"],
        locations=search_params["locations"],
        sources=search_params["sources"],
        results_per_keyword=search_params.get("results_per_keyword", 25),
        max_age_hours=search_params.get("max_age_hours", 24),
        country_indeed=search_params.get("country_indeed", "India"),
        linkedin_fetch_description=search_params.get("linkedin_fetch_description", True),
        search_delay_sec=search_params.get("search_delay_sec", 15),
        linkedin_max_retries=search_params.get("linkedin_max_retries", 2),
        linkedin_retry_delay_sec=search_params.get("linkedin_retry_delay_sec", 30),
        enable_naukri_auto=search_params.get("enable_naukri_auto", False),
        manual_naukri_jobs=search_params.get("manual_naukri_jobs", []),
        enable_naukri_apify=search_params.get("enable_naukri_apify", False),
        apify_naukri_actor_id=search_params.get("apify_naukri_actor_id", ""),
        apify_api_token=search_params.get("apify_api_token") or "",
        progress=progress_cb,
    )

    if not jobs:
        progress_cb("step 2/3: skipped — no jobs to score")
        progress_cb("step 3/3: skipped — no jobs to apply to")
        return {"jobs_scraped": 0, "score_result": None, "apply_result": None}

    progress_cb("step 2/3: scoring")
    score_result = scorer.score_jobs(
        jobs,
        resume_summary=score_params["resume_summary"],
        skills=score_params["skills"],
        career_targets=score_params.get("career_targets", []),
        keyword_prefilter_min_pct=score_params.get("keyword_prefilter_min_pct", 30),
        llm_top_n_to_rank=score_params.get("llm_top_n_to_rank", 40),
        llm_min_score_to_keep=score_params.get("llm_min_score_to_keep", 70),
        gemini_model=score_params.get("gemini_model", "gemini-2.5-flash"),
        hf_model=score_params.get("hf_model", "Qwen/Qwen3-8B"),
        gemini_api_key=score_params.get("gemini_api_key") or settings.GEMINI_API_KEY,
        hf_api_key=score_params.get("hf_api_key") or settings.HF_API_KEY,
        progress=progress_cb,
    )

    apply_result = None
    if apply_params and score_result["ranked_jobs"]:
        progress_cb("step 3/3: opening browser for top matches")
        from app.core.display import resolve_headless

        actual_headless, warning = resolve_headless(apply_params.get("headless", False))
        if warning:
            progress_cb(warning)

        raw = browser_apply.open_and_prepare(
            score_result["ranked_jobs"],
            applicant_info=apply_params["applicant_info"],
            open_top_n=apply_params.get("open_top_n", 10),
            auto_fill_easy_apply=apply_params.get("auto_fill_easy_apply", True),
            pause_between_tabs_sec=apply_params.get("pause_between_tabs_sec", 2),
            browser_profile_dir=apply_params.get("browser_profile_dir") or str(settings.BROWSER_PROFILE_DIR),
            applications_log_path=settings.OUTPUT_DIR / "applications_log.csv",
            headless=actual_headless,
            require_auto_apply_capable=apply_params.get("require_auto_apply_capable", True), 
            progress=progress_cb,
        )
        raw.pop("handle", None)  # not JSON-serializable — see apply_task's note
        raw["headless_used"] = actual_headless
        apply_result = raw
    else:
        progress_cb("step 3/3: skipped (no apply config provided)")

    return {
        "jobs_scraped": len(jobs),
        "score_result": score_result,
        "apply_result": apply_result,
    }