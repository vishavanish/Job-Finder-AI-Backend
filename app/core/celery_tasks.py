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

LLM scoring goes through the AI router (app/services/ai_router.py): the
model chain comes from the registry, keys from the key store. Tasks only
carry `user_id` and, for legacy request-body keys, short-lived key refs.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.services import browser_apply, company_ats_sources, job_sources, scorer
# --- add near the top, alongside existing imports ---
from celery import group, chord
from app.services import job_store, pipeline_progress, search_profiles, secret_refs
from app.services.job_dedupe import job_key
from app.core.db import SessionLocal
from app.services import provider_catalog
from app.services.scorer import keyword_score
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

def _request_keys(score_params: dict) -> dict[str, str]:
    """Legacy one-run keys sent in a /score or /pipeline body, by key scope.
    No .env fallback here: the key store handles platform/.env keys."""
    return {
        "gemini": secret_refs.resolve(score_params.get("gemini_api_key_ref")),
        "groq": secret_refs.resolve(score_params.get("groq_api_key_ref")),
        "huggingface": secret_refs.resolve(score_params.get("hf_api_key_ref")),
    }


def _apify_token(db, search_params: dict) -> str:
    """Request-body token first (legacy), then the user's/platform/.env key."""
    from app.services import key_store
    token = secret_refs.resolve(search_params.get("apify_api_token_ref"))
    if token:
        return token
    resolved = key_store.resolve(db, user_id=search_params.get("user_id"), scope="apify")
    return resolved.value if resolved else ""


def _score_one_job_llm(job: dict, *, db, score_params: dict, request_keys: dict[str, str], ref: str | None) -> dict:
    """Scores exactly ONE job via the router (per-job so the streaming
    pipeline can show results live). One LLM call per job costs more than
    batching; if that matters, batch per keyword instead."""
    return scorer.score_one_job(
        db, job, user_id=score_params.get("user_id"), resume_summary=score_params["resume_summary"],
        career_targets=score_params.get("career_targets", []), request_keys=request_keys, ref=ref,
    )


def _inbox_jobs(db, user_id: str | None, sources: list[str], search_params: dict) -> list[dict]:
    """The user's job-alert email jobs (P5), only when that source runs."""
    if not user_id or "email_alerts" not in sources:
        return []
    from app.services import email_sync
    return email_sync.recent_alert_jobs(db, user_id, search_params.get("max_age_hours", 24))


def _index_lookup(db, sources: list[str], search_params: dict, locations: list[str]):
    """The ATS index search for the `company_ats` source (P6), or None when that source doesn't run."""
    if "company_ats" not in sources:
        return None
    from app.services import ats_search

    def lookup(keyword: str) -> list[dict]:
        return ats_search.index_jobs(db, keyword, locations=locations,
                                     country=search_params.get("country_indeed", "India"),
                                     max_age_hours=search_params.get("max_age_hours", 24),
                                     limit=search_params.get("results_per_keyword", 25))
    return lookup


def _harvest_ats(db, jobs: list[dict]) -> None:
    """Apply links on a supported ATS grow the ATS Discovery registry (P6). Never fails a search."""
    from app.services import ats_registry
    try:
        added = ats_registry.harvest(db, jobs)
        if added:
            logger.info("ats registry: %s companies found in search results", added)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("ats harvest failed")


def _scrape(db, search_params: dict, *, keywords: list[str], locations: list[str], sources: list[str],
            manual_naukri_jobs: list, progress) -> list[dict]:
    """job_sources.scrape_all with the params a route built, plus source
    health: sources that answered are marked healthy, failures counted
    (enough in a row pauses the source — provider_catalog.paused_until).
    Free-source keys are looked up here, in the worker, never enqueued."""
    from app.services import key_store
    for note in search_params.get("skipped_sources") or []:
        progress(f"skipped source — {note}")
    naukri_apify = search_params.get("enable_naukri_apify", False)
    user_id = search_params.get("user_id")
    healthy: set[str] = set()
    failed: dict[str, str] = {}

    def on_source_result(key: str, error: str | None) -> None:
        if error is None:
            healthy.add(key)
        else:
            failed[key] = error

    jobs = job_sources.scrape_all(
        keywords=keywords,
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
        manual_naukri_jobs=manual_naukri_jobs,
        inbox_jobs=_inbox_jobs(db, user_id, sources, search_params),
        index_jobs=_index_lookup(db, sources, search_params, locations),
        enable_naukri_apify=naukri_apify,
        apify_naukri_actor_id=search_params.get("apify_naukri_actor_id", ""),
        apify_api_token=_apify_token(db, search_params) if naukri_apify else "",
        user_id=user_id,
        client_ip=search_params.get("client_ip", ""),
        client_user_agent=search_params.get("client_user_agent", ""),
        key_lookup=lambda scope, platform_only=False: key_store.resolve(
            db, user_id=None if platform_only else user_id, scope=scope),
        on_source_result=on_source_result,
        progress=progress,
    )
    try:
        provider_catalog.mark_success(db, healthy)
        for key, error in failed.items():
            provider_catalog.mark_failure(db, key, error)
    except Exception:  # noqa: BLE001 — health bookkeeping must never fail a search
        db.rollback()
        logger.exception("could not record source health")
    _harvest_ats(db, jobs)
    return jobs


def _score_jobs(db, jobs: list[dict], score_params: dict, *, ref: str | None, progress) -> dict:
    return scorer.score_jobs(
        db,
        jobs,
        user_id=score_params.get("user_id"),
        resume_summary=score_params["resume_summary"],
        skills=score_params["skills"],
        career_targets=score_params.get("career_targets", []),
        keyword_prefilter_min_pct=score_params.get("keyword_prefilter_min_pct", 30),
        llm_top_n_to_rank=score_params.get("llm_top_n_to_rank", 40),
        llm_min_score_to_keep=score_params.get("llm_min_score_to_keep", 70),
        request_keys=_request_keys(score_params),
        ref=ref,
        progress=progress,
    )


def _prefilter_score(job: dict, score_params: dict) -> float:
    """Weighted skill terms (search profile) or the plain skills list (legacy)."""
    terms = score_params.get("skill_terms")
    if terms:
        return search_profiles.weighted_keyword_score(search_profiles.job_text(job), terms)
    return keyword_score(job.get("description", ""), score_params["skills"])


def _process_job(db, run_id: str, job: dict, *, score_params: dict, request_keys: dict[str, str]) -> str:
    """One scraped copy through dedupe -> filters -> seen check -> score ->
    feed (CLAUDE.md 7.5). Returns what happened to it."""
    user_id = score_params.get("user_id")
    key = job_key(job)
    canonical = job_store.upsert_copy(db, job, key)

    # Claim before any scoring: another keyword task in this run may
    # already have this job. If so, it may still need our better copy.
    if not pipeline_progress.claim_job_key(run_id=run_id, job_key=key):
        _replace_if_better(db, run_id, key, canonical)
        return "duplicates"

    best = job_store.as_job(canonical)
    if search_profiles.blocked_reason(best, score_params.get("filters") or {}):
        return "blocked"

    phash = score_params.get("profile_hash")
    uj = job_store.get_user_job(db, user_id, canonical.id) if user_id else None
    decision = job_store.decide(uj, include_seen=bool(score_params.get("include_seen")), profile_hash=phash)
    if decision == "skip":
        return "seen"

    def remember(state: str, kw: float | None, llm: float | None, reason: str | None,
                 breakdown: dict | None = None) -> None:
        if user_id:
            job_store.record_user_job(db, user_id=user_id, job_id=canonical.id, state=state, keyword_score=kw,
                                      llm_score=llm, reason=reason, profile_hash=phash,
                                      search_profile_id=score_params.get("search_profile_id"), run_id=run_id,
                                      breakdown=breakdown)

    if decision == "reuse":
        scored = {**best, "keyword_score": uj.keyword_score, "llm_score": uj.llm_score, "llm_reason": uj.reason or "",
                  "match": uj.breakdown or search_profiles.match_breakdown(best, score_params)}
    else:
        best["keyword_score"] = _prefilter_score(best, score_params)
        if best["keyword_score"] < score_params.get("keyword_prefilter_min_pct", 30):
            # cheap prefilter still applies — no point burning an LLM call on a clear non-match
            remember("filtered", best["keyword_score"], None, None)
            return "filtered"
        scored = _score_one_job_llm(best, db=db, score_params=score_params, request_keys=request_keys, ref=run_id)
        if scored.get("llm_failed"):
            # Not a low score — no model answered. Record nothing so the next run retries it.
            return "unscored"
        if scored.get("llm_score", 0) < score_params.get("llm_min_score_to_keep", 70):
            remember("filtered", scored.get("keyword_score"), scored.get("llm_score"), scored.get("llm_reason"))
            return "filtered"
        scored["match"] = {**search_profiles.match_breakdown(best, score_params),
                           "seniority": scored.get("llm_seniority"),
                           "missing_required": scored.get("llm_missing")}

    scored.update(job_store.freshness(canonical, score_params.get("previous_search_at")))
    scored["auto_apply_capable"] = scored.get("source") in AUTO_APPLY_CAPABLE_SOURCES
    payload = pipeline_progress.record_scored_job(run_id=run_id, job=scored)
    pipeline_progress.remember_recorded(run_id=run_id, job_key=key, payload=payload)
    remember("seen", scored.get("keyword_score"), scored.get("llm_score"), scored.get("llm_reason"),
             breakdown=scored.get("match"))
    # A better copy may have been stored by another task while we scored.
    _replace_if_better(db, run_id, key, job_store.reload(db, canonical))
    return "kept"


def add_applicant_counts(db, run_id: str | None, keys: list[str], *, progress=lambda m: None,
                         sleep=time.sleep) -> int:
    """LinkedIn applicant counts for jobs just shown (P3.2): at most
    APPLICANT_COUNT_MAX_PER_RUN pages, 2-4 s apart, re-checked after 24 h,
    stopped at the first 429 or when LinkedIn is paused/off. Updates the
    canonical job and, for a pipeline run, its feed entry. Returns how
    many counts were found."""
    from app.models.ai import ProviderCatalog
    from app.models.jobs import Job
    from app.providers.job_sources import linkedin_applicants

    row = db.get(ProviderCatalog, "linkedin")
    if row is None or not row.enabled_global or provider_catalog.paused_until(row):
        return 0
    stale = datetime.now(timezone.utc) - timedelta(hours=24)
    found = checked = 0
    for key in keys:
        if checked >= settings.APPLICANT_COUNT_MAX_PER_RUN:
            break
        job = db.query(Job).filter(Job.job_key == key).first()
        if job is None or (job.best_source or "").lower() != "linkedin":
            continue
        last = job.applicants_checked_at
        if last is not None and (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) > stale:
            continue
        if checked:
            sleep(random.uniform(2, 4))
        checked += 1
        try:
            count = linkedin_applicants.fetch_count(job.url)
        except linkedin_applicants.RateLimited:
            progress("LinkedIn applicant counts: rate-limited — stopping for this run")
            break
        job.applicants_checked_at = datetime.now(timezone.utc)
        if count:
            job.applicants = count
            found += 1
        db.commit()
        if count and run_id:
            pipeline_progress.update_recorded(run_id=run_id, job_key=key, fields={"applicants": count})
    return found


def _replace_if_better(db, run_id: str, key: str, canonical) -> None:
    best = job_store.as_job(canonical)
    best["auto_apply_capable"] = best["source"] in AUTO_APPLY_CAPABLE_SOURCES
    pipeline_progress.replace_recorded(run_id=run_id, job_key=key, best=best)


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

    if pipeline_progress.is_cancelled(run_id):
        pipeline_progress.increment_keyword_completed(run_id=run_id)
        return {"keyword": keyword, "skipped": "run cancelled"}

    db = SessionLocal()
    try:
        raw_jobs = _scrape(db, search_params, keywords=[keyword], locations=locations, sources=sources,
                           # manual jobs are added once at the top level, not per keyword
                           manual_naukri_jobs=[], progress=progress_cb)

        request_keys = _request_keys(score_params)
        counts = {"duplicates": 0, "blocked": 0, "seen": 0, "filtered": 0, "unscored": 0, "kept": 0}
        kept_keys: list[str] = []
        for job in raw_jobs:
            outcome = _process_job(db, run_id, job, score_params=score_params, request_keys=request_keys)
            if outcome in counts:
                counts[outcome] += 1
            if outcome == "kept":
                kept_keys.append(job_key(job))
                progress_cb(f"'{keyword}': scored+kept {job.get('title', '')}")
        if "linkedin" in sources:
            add_applicant_counts(db, run_id, kept_keys, progress=progress_cb)

        if counts["duplicates"]:
            progress_cb(f"'{keyword}': skipped {counts['duplicates']} job(s) already handled in this run")
        if counts["seen"]:
            progress_cb(f"'{keyword}': skipped {counts['seen']} job(s) you have already seen")
        if counts["blocked"]:
            progress_cb(f"'{keyword}': dropped {counts['blocked']} job(s) by your must-have/exclude/avoid rules")
        if counts["filtered"]:
            progress_cb(f"'{keyword}': {counts['filtered']} job(s) scored below your thresholds")
        if counts["unscored"]:
            progress_cb(f"'{keyword}': {counts['unscored']} job(s) not scored — every AI model failed; "
                        "they will be retried next run")
        pipeline_progress.increment_keyword_completed(run_id=run_id)
        return {"keyword": keyword, "raw_found": len(raw_jobs), **counts}

    except Exception as e:  # noqa: BLE001
        logger.exception("search_and_score_keyword failed for run_id=%s keyword=%s", run_id, keyword)
        pipeline_progress.increment_keyword_completed(run_id=run_id)  # still counts as "done" so the run can finish
        return {"keyword": keyword, "error": str(e)}
    finally:
        db.close()


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
    
    db = SessionLocal()
    try:
        jobs = _scrape(db, search_params, keywords=search_params["keywords"], locations=search_params["locations"],
                       sources=search_params["sources"],
                       manual_naukri_jobs=search_params.get("manual_naukri_jobs", []), progress=progress_cb)
    finally:
        db.close()
    return {"jobs": jobs, "total_raw": len(jobs), "total_usable": len(jobs)}


@celery_app.task(bind=True, name="job_finder.score")
def score_task(self, jobs: list[dict], score_params: dict) -> dict:
    settings = get_settings()
    progress_cb = _make_progress_cb(self, "score")
    db = SessionLocal()
    try:
        return _score_jobs(db, jobs, score_params, ref=self.request.id, progress=progress_cb)
    finally:
        db.close()


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

    result = browser_apply.open_and_prepare_isolated(
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
    db = SessionLocal()
    try:
        jobs = _scrape(db, search_params, keywords=search_params["keywords"], locations=search_params["locations"],
                       sources=search_params["sources"],
                       manual_naukri_jobs=search_params.get("manual_naukri_jobs", []), progress=progress_cb)
        if not jobs:
            progress_cb("step 2/3: skipped — no jobs to score")
            progress_cb("step 3/3: skipped — no jobs to apply to")
            return {"jobs_scraped": 0, "score_result": None, "apply_result": None}

        progress_cb("step 2/3: scoring")
        score_result = _score_jobs(db, jobs, score_params, ref=self.request.id, progress=progress_cb)
    finally:
        db.close()

    apply_result = None
    if apply_params and score_result["ranked_jobs"]:
        progress_cb("step 3/3: opening browser for top matches")
        from app.core.display import resolve_headless

        actual_headless, warning = resolve_headless(apply_params.get("headless", False))
        if warning:
            progress_cb(warning)

        raw = browser_apply.open_and_prepare_isolated(
            score_result["ranked_jobs"],
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

def _engine_step(step: str, run_id: str) -> dict:
    """Runs apply_engine.prepare/submit in a fresh thread with its own DB
    session (Playwright's sync API can't run inside an event loop)."""
    from app.services import apply_engine

    def work() -> dict:
        db = SessionLocal()
        try:
            run = getattr(apply_engine, step)(db, run_id)
            return {"run_id": run_id, "state": run.state if run is not None else "missing"}
        finally:
            db.close()

    return apply_engine.run_isolated(work)


@celery_app.task(name="job_finder.apply_prepare")
def apply_prepare_task(run_id: str) -> dict:
    """Apply engine step 1 (P4b): open the form, answer it, stop for the user."""
    return _engine_step("prepare", run_id)


@celery_app.task(name="job_finder.apply_submit")
def apply_submit_task(run_id: str) -> dict:
    """Apply engine step 2 (P4b): fill the approved answers and submit."""
    return _engine_step("submit", run_id)


@celery_app.task(name="job_finder.purge_apply_proofs")
def purge_apply_proofs_task() -> dict:
    """Deletes proof screenshots older than APPLY_PROOF_RETENTION_DAYS."""
    from app.services import apply_engine

    db = SessionLocal()
    try:
        return {"deleted": apply_engine.purge_old_proofs(db)}
    finally:
        db.close()


@celery_app.task(name="job_finder.gmail_tick")
def gmail_tick_task() -> dict:
    """Beat (P5): claims Gmail accounts that are due and queues one sync each on the email queue."""
    from app.services import email_sync

    db = SessionLocal()
    try:
        claimed = email_sync.claim_due(db)
    finally:
        db.close()
    for account_id in claimed:
        sync_gmail_account_task.apply_async(args=[account_id], queue=get_settings().email_queue)
    return {"queued": len(claimed)}


@celery_app.task(name="job_finder.sync_gmail_account")
def sync_gmail_account_task(account_id: str) -> dict:
    """Reads new mail for one Gmail account: replies update applications, job alerts become jobs (P5)."""
    from app.services import email_sync

    db = SessionLocal()
    try:
        return email_sync.sync_account(db, account_id)
    finally:
        db.close()


@celery_app.task(name="job_finder.ats_tick")
def ats_tick_task() -> dict:
    """Beat (P6): loads the seed once when the registry is empty, then queues a crawl for each due company."""
    from app.models.ats import AtsCompany
    from app.services import ats_crawler, ats_registry

    db = SessionLocal()
    try:
        if db.query(AtsCompany.id).first() is None:
            ats_registry.seed(db)
        claimed = ats_crawler.claim_due(db)
    finally:
        db.close()
    for company_id in claimed:
        crawl_ats_company_task.apply_async(args=[company_id], queue=get_settings().maintenance_queue)
    return {"queued": len(claimed)}


@celery_app.task(name="job_finder.crawl_ats_company")
def crawl_ats_company_task(company_id: str) -> dict:
    """Refreshes one company's open jobs in the ATS index (P6)."""
    from app.services import ats_crawler

    db = SessionLocal()
    try:
        return ats_crawler.crawl_company(db, company_id)
    finally:
        db.close()


@celery_app.task(bind=True, name="job_finder.ats_topup")
def ats_topup_task(self, query: str, user_id: str) -> dict:
    """Live top-up for a title with few index matches (P6): searches / refreshes stale companies."""
    from app.services import ats_search

    return ats_search.topup(SessionLocal, query, progress=_make_progress_cb(self, "ats_topup"))


@celery_app.task(name="job_finder.ats_expand_title")
def ats_expand_title_task(query: str, user_id: str) -> dict:
    """Caches other titles for the same role (AI router), used by the next ATS search (P6)."""
    from app.services import ats_search

    db = SessionLocal()
    try:
        return {"titles": ats_search.expand_with_ai(db, query, user_id)}
    finally:
        db.close()


@celery_app.task(name="job_finder.purge_ats_jobs")
def purge_ats_jobs_task() -> dict:
    """Deletes ATS index jobs closed more than ATS_CLOSED_RETENTION_DAYS ago (P6)."""
    from app.services import ats_crawler

    db = SessionLocal()
    try:
        return {"deleted": ats_crawler.purge_closed(db)}
    finally:
        db.close()


@celery_app.task(name="job_finder.purge_email_data")
def purge_email_data_task() -> dict:
    """Deletes email events older than EMAIL_EVENT_RETENTION_DAYS and alert jobs older than EMAIL_JOBS_RETENTION_DAYS."""
    from app.services import email_sync

    db = SessionLocal()
    try:
        return email_sync.purge_old(db)
    finally:
        db.close()


@celery_app.task(name="job_finder.build_apply_pack")
def build_apply_pack_task(pack_id: str, user_id: str) -> dict:
    """Builds one prep pack (P4a): tailored resume + cover letter + email
    draft. Resume and job text are read from the DB here, never enqueued."""
    from app.models.apply import ApplyPack
    from app.services import apply_prep

    db = SessionLocal()
    try:
        pack = db.query(ApplyPack).filter(ApplyPack.id == pack_id, ApplyPack.user_id == user_id).first()
        if pack is None:
            return {"pack_id": pack_id, "status": "missing"}
        try:
            apply_prep.build_pack(db, pack)
        except Exception:  # noqa: BLE001 — a crash must not leave the pack "pending" forever
            db.rollback()
            logger.exception("prep pack crashed for pack=%s", pack_id)
            pack = db.get(ApplyPack, pack_id)
            if pack is None:
                return {"pack_id": pack_id, "status": "missing"}
            pack.status, pack.error = "failed", "Preparing the application failed. Try again."
        db.commit()
        return {"pack_id": pack_id, "status": pack.status}
    finally:
        db.close()


@celery_app.task(name="job_finder.parse_resume")
def parse_resume_task(resume_id: str, user_id: str) -> dict:
    """AI-parses one resume (CLAUDE.md 7.2). The text is read from the DB
    here — resume content never travels through the broker."""
    from app.models.profile import Resume
    from app.services import resume_store

    db = SessionLocal()
    try:
        row = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user_id).first()
        if row is None:
            return {"resume_id": resume_id, "status": "missing"}
        try:
            resume_store.parse_resume(db, row)
        except Exception:  # noqa: BLE001 — a crash must not leave the resume "pending" forever
            db.rollback()
            logger.exception("resume parse crashed for resume=%s", resume_id)
            row = db.get(Resume, resume_id)
            if row is None:
                return {"resume_id": resume_id, "status": "missing"}
            row.parse_status, row.parse_error = "failed", "Reading the resume failed. Try again."
        db.commit()
        return {"resume_id": resume_id, "status": row.parse_status}
    finally:
        db.close()


# ============================================================
# JOB ALERTS (P3.2)
# ============================================================

@celery_app.task(name="job_finder.alerts_tick")
def alerts_tick() -> dict:
    """Beat, every JOB_ALERT_TICK_SECONDS: start every alert that is due."""
    from app.services import job_alerts
    db = SessionLocal()
    try:
        won = job_alerts.claim_due(db)
    finally:
        db.close()
    for alert_id in won:
        run_job_alert.apply_async(args=[alert_id, "schedule"], queue=settings.default_queue)
    return {"started": len(won)}


@celery_app.task(bind=True, name="job_finder.run_job_alert")
def run_job_alert(self, alert_id: str, trigger: str = "schedule") -> dict:
    """One alert run: the alert's search profile through the normal pipeline
    path, then the digest. Results are also a normal pipeline run (`run_id`
    in counts), so GET /pipeline/{run_id}/jobs shows them too."""
    from app.models.alerts import JobAlert, JobAlertRun
    from app.models.schemas import PipelineRequest
    from app.models.user import User
    from app.providers.email import get_email_sender
    from app.services import job_alerts
    from app.services.task_params import build_pipeline_params
    from fastapi import HTTPException

    progress_cb = _make_progress_cb(self, "run_job_alert")
    db = SessionLocal()
    run = None
    try:
        alert = db.get(JobAlert, alert_id)
        if alert is None:
            return {"skipped": "alert deleted"}
        user = db.get(User, alert.user_id)
        run = JobAlertRun(alert_id=alert.id, user_id=alert.user_id, trigger=trigger, counts={}, job_ids=[])
        db.add(run)
        db.commit()

        reason = job_alerts.may_run(db, user)
        if reason:
            run.status, run.error, run.finished_at = "skipped", reason, job_alerts.utcnow()
            db.commit()
            return {"skipped": reason}

        try:
            search_params, score_params = build_pipeline_params(
                PipelineRequest(search_profile_id=alert.search_profile_id), db=db, user=user)
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
            raise RuntimeError(detail.get("message") or "the search could not be prepared") from e
        score_params["llm_min_score_to_keep"] = alert.min_score
        score_params["previous_search_at"] = pipeline_progress.mark_user_search(user.id)

        keywords = search_params["keywords"]
        run_id = pipeline_progress.create_run(user_id=user.id, total_keywords=len(keywords), kind="alert")
        request_keys = _request_keys(score_params)
        counts = {"run_id": run_id, "raw": 0, "kept": 0, "duplicates": 0, "blocked": 0, "seen": 0, "filtered": 0}
        kept_keys: list[str] = []
        for keyword in keywords:
            raw_jobs = _scrape(db, search_params, keywords=[keyword], locations=search_params["locations"],
                               sources=search_params["sources"], manual_naukri_jobs=[], progress=progress_cb)
            counts["raw"] += len(raw_jobs)
            for job in raw_jobs:
                outcome = _process_job(db, run_id, job, score_params=score_params, request_keys=request_keys)
                if outcome in counts:
                    counts[outcome] += 1
                if outcome == "kept":
                    kept_keys.append(job_key(job))
            pipeline_progress.increment_keyword_completed(run_id=run_id)
        if "linkedin" in search_params["sources"]:
            add_applicant_counts(db, run_id, kept_keys, progress=progress_cb)

        latest: dict[str, dict] = {}
        for entry in pipeline_progress.get_jobs(run_id=run_id):
            latest[entry["id"]] = entry  # replacements share the id; keep the newest copy
        jobs = sorted(latest.values(), key=lambda j: j.get("llm_score") or 0, reverse=True)
        run.job_ids = [j["id"] for j in jobs]
        run.counts = counts
        run.status, run.finished_at = "done", job_alerts.utcnow()
        alert.last_run_at = run.finished_at
        db.commit()

        if jobs and alert.email_digest:
            subject, body = job_alerts.digest_text(alert, jobs)
            try:
                get_email_sender().send(to=user.email, subject=subject, body=body)
                run.emailed_at = job_alerts.utcnow()
            except Exception as e:  # noqa: BLE001 — the run's results stay in the app
                logger.warning("alert %s: digest email failed: %s", alert.id, type(e).__name__)
                run.error = f"digest email failed: {type(e).__name__}"
            db.commit()
        return {"alert_id": alert.id, "run_id": run_id, "new_jobs": len(jobs)}
    except Exception as e:  # noqa: BLE001
        logger.exception("job alert %s failed", alert_id)
        db.rollback()
        if run is not None:
            run.status, run.error, run.finished_at = "failed", str(e)[:500], datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
        return {"alert_id": alert_id, "error": str(e)[:200]}
    finally:
        db.close()


@celery_app.task(name="job_finder.voice_finalize")
def voice_finalize_task(session_id: str) -> dict:
    """After a voice session (P7): ends it if its socket dropped and wasn't
    resumed, then scores the answers. Transcripts are read from the DB here,
    never sent through the broker."""
    from app.services import voice_eval

    db = SessionLocal()
    try:
        return voice_eval.finalize(db, session_id)
    except Exception:  # noqa: BLE001 — a crash must not leave feedback "pending" forever
        db.rollback()
        logger.exception("voice finalize crashed for session=%s", session_id)
        from app.models.voice import VoiceSession
        row = db.get(VoiceSession, session_id)
        if row is not None and row.feedback_state == "pending":
            row.feedback_state = "failed"
            db.commit()
        return {"status": "failed"}
    finally:
        db.close()


@celery_app.task(name="job_finder.purge_voice_data")
def purge_voice_data_task() -> dict:
    """Deletes voice sessions past VOICE_TRANSCRIPT_RETENTION_DAYS (P7)."""
    from app.services import voice_store

    db = SessionLocal()
    try:
        return {"deleted": voice_store.purge_expired(db)}
    finally:
        db.close()


@celery_app.task(name="job_finder.story_bank_draft")
def story_bank_draft_task(user_id: str) -> dict:
    """Drafts STAR stories from the user's master resume (P7); drafts wait for approval."""
    from app.services import ai_router, story_bank

    db = SessionLocal()
    try:
        return story_bank.draft_from_resume(db, user_id)
    except ai_router.NoModelAvailable as e:
        return {"created": 0, "message": f"No AI model could draft stories: {e}"[:300]}
    finally:
        db.close()
