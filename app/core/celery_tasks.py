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
# --- add near the top, alongside existing imports ---
from celery import group, chord
from app.services import pipeline_progress
from app.services.scorer import (
    keyword_score, _build_user_prompt, _parse_and_apply_scores,
    LLM_SYSTEM_PROMPT_TEMPLATE,
)
from app.services.browser_apply import AUTO_APPLY_CAPABLE_SOURCES
logger = logging.getLogger("job_finder_api.tasks")

settings = get_settings()
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

def _score_one_job_llm(
    job: dict, *, resume_summary: str, career_targets: list[str],
    gemini_model: str, hf_model: str, gemini_api_key: str, hf_api_key: str,
) -> dict:
    """Scores exactly ONE job via the LLM (not a batch). Reuses scorer.py's
    existing prompt-building and response-parsing helpers so scoring
    logic isn't duplicated/forked between the batched and per-job paths
    — this just calls them with a list of length 1.

    Trade-off (flagged for the record, not hidden): one LLM API call per
    job is materially more expensive and slower in aggregate than the
    original batched approach (~40 jobs/call). This function exists
    because per-job streaming was the explicit requirement — if cost
    becomes a problem, the fix is switching to per-keyword-batch scoring
    (still parallel across keywords via the same group() structure),
    not changing this file's architecture."""
    jobs_wrapper = [job]
    system_prompt = LLM_SYSTEM_PROMPT_TEMPLATE.format(
        career_targets="\n        ".join(f"- {t}" for t in career_targets)
    )
    user_prompt = _build_user_prompt(jobs_wrapper, resume_summary)

    if gemini_api_key:
        try:
            from google import genai
            client = genai.Client(api_key=gemini_api_key)
            response = client.models.generate_content(
                model=gemini_model,
                contents=user_prompt,
                config={"system_instruction": system_prompt, "temperature": 0.1,
                        "response_mime_type": "application/json"},
            )
            if response.text and _parse_and_apply_scores(response.text, jobs_wrapper, lambda m: None):
                return jobs_wrapper[0]
        except Exception:  # noqa: BLE001
            logger.exception("gemini per-job scoring failed for url=%s — falling back to HF", job.get("url"))

    if hf_api_key:
        try:
            from huggingface_hub import InferenceClient
            hf_client = InferenceClient(api_key=hf_api_key)
            response = hf_client.chat_completion(
                model=hf_model,
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user_prompt}],
                max_tokens=500, temperature=0.1,
            )
            raw_text = response.choices[0].message.content.strip()
            if _parse_and_apply_scores(raw_text, jobs_wrapper, lambda m: None):
                return jobs_wrapper[0]
        except Exception:  # noqa: BLE001
            logger.exception("HF per-job scoring failed for url=%s", job.get("url"))

    job.setdefault("llm_score", 0)
    job.setdefault("llm_reason", "LLM scoring unavailable for this job.")
    return job


@celery_app.task(bind=True, name="job_finder.search_and_score_keyword")
def search_and_score_keyword(
    self, run_id: str, keyword: str, locations: list[str], sources: list[str],
    search_params: dict, score_params: dict,
) -> dict:
    """ONE group() member = ONE keyword. Searches that keyword across
    all configured locations/sources, then scores each resulting job
    INDIVIDUALLY and writes it to PipelineRunJob the instant it's
    scored — this is what makes GET /pipeline/{run_id}/jobs show
    results while other keywords are still running."""
    progress_cb = _make_progress_cb(self, "search_and_score_keyword")
    settings = get_settings()

    try:
        raw_jobs = job_sources.scrape_all(
            keywords=[keyword],
            locations=locations,
            sources=sources,
            results_per_keyword=search_params.get("results_per_keyword", 25),
            max_age_hours=search_params.get("max_age_hours", 24),
            country_indeed=search_params.get("country_indeed", "India"),
            linkedin_fetch_description=search_params.get("linkedin_fetch_description", True),
            search_delay_sec=search_params.get("search_delay_sec", 15),
            linkedin_max_retries=search_params.get("linkedin_max_retries", 2),
            linkedin_retry_delay_sec=search_params.get("linkedin_retry_delay_sec", 30),
            enable_naukri_auto=search_params.get("enable_naukri_auto", False),
            manual_naukri_jobs=[],  # applied once at the top level, not per-keyword — avoids duplicate inserts
            enable_naukri_apify=search_params.get("enable_naukri_apify", False),
            apify_naukri_actor_id=search_params.get("apify_naukri_actor_id", ""),
            apify_api_token=search_params.get("apify_api_token") or "",
            progress=progress_cb,
        )

        skills = score_params["skills"]
        min_pct = score_params.get("keyword_prefilter_min_pct", 30)
        gemini_api_key = score_params.get("gemini_api_key") or settings.GEMINI_API_KEY
        hf_api_key = score_params.get("hf_api_key") or settings.HF_API_KEY

        scored_count = 0
        for job in raw_jobs:
            job["keyword_score"] = keyword_score(job.get("description", ""), skills)
            if job["keyword_score"] < min_pct:
                continue  # cheap prefilter still applies — no point burning an LLM call on a clear non-match

            scored = _score_one_job_llm(
                job,
                resume_summary=score_params["resume_summary"],
                career_targets=score_params.get("career_targets", []),
                gemini_model=score_params.get("gemini_model", "gemini-2.5-flash"),
                hf_model=score_params.get("hf_model", "Qwen/Qwen3-8B"),
                gemini_api_key=gemini_api_key,
                hf_api_key=hf_api_key,
            )

            if scored.get("llm_score", 0) < score_params.get("llm_min_score_to_keep", 70):
                continue

            scored["auto_apply_capable"] = scored.get("source") in AUTO_APPLY_CAPABLE_SOURCES
            pipeline_progress.record_scored_job(run_id=run_id, job=scored)
            scored_count += 1
            progress_cb(f"'{keyword}': scored+kept {scored['title']} ({scored['llm_score']})")

        pipeline_progress.increment_keyword_completed(run_id=run_id)
        return {"keyword": keyword, "raw_found": len(raw_jobs), "kept": scored_count}

    except Exception as e:  # noqa: BLE001
        logger.exception("search_and_score_keyword failed for run_id=%s keyword=%s", run_id, keyword)
        pipeline_progress.increment_keyword_completed(run_id=run_id)  # still counts as "done" so the run can finish
        return {"keyword": keyword, "error": str(e)}


@celery_app.task(name="job_finder.finalize_pipeline_run")
def finalize_pipeline_run(results: list[dict], run_id: str) -> dict:
    """Chord callback — fires once ALL group() members have finished.
    Marks the run 'completed' (or 'failed' if every single keyword
    errored) so the frontend's progress bar/poll can stop."""
    all_failed = results and all("error" in r for r in results)
    pipeline_progress.mark_run_status(
        run_id=run_id,
        status="failed" if all_failed else "completed",
        error="all keyword searches failed" if all_failed else None,
    )
    return {"run_id": run_id, "keyword_results": results}

@celery_app.task(name="job_finder.dispatch_pipeline_group")
def dispatch_pipeline_group(run_id: str, search_params: dict, score_params: dict) -> None:
    """Plain group() — NOT chord(). Upstash's Redis (serverless/free tier)
    does not reliably support the extra bookkeeping commands Celery's
    chord-unlock mechanism depends on to detect "all group members
    finished" and fire finalize_pipeline_run automatically. On Upstash
    this can silently fail to dispatch the group AT ALL, which is why
    logs showed dispatch_pipeline_group succeeding but
    search_and_score_keyword never running.

    Fix: dispatch a plain group (no callback), and let each keyword task
    mark its own completion via increment_keyword_completed(). The route
    layer (GET /pipeline/{run_id}) derives "is the run done" by comparing
    keywords_completed to total_keywords — no chord callback needed."""
    keywords = search_params["keywords"]
    job = group(
        search_and_score_keyword.s(
            run_id, kw, search_params["locations"], search_params["sources"],
            search_params, score_params,
        )
        for kw in keywords
    )
    job.apply_async(queue=settings.default_queue)


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