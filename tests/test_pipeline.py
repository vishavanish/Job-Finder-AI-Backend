from app.core import celery_tasks
from app.services import pipeline_progress

DUPLICATE_JOB = {"source": "LinkedIn", "title": "AI Engineer", "company": "Acme",
                 "location": "Pune", "url": "https://linkedin.example/1", "description": "python llm"}
SCORE_PARAMS = {"resume_summary": "AI engineer", "skills": ["python"],
                "keyword_prefilter_min_pct": 0, "llm_min_score_to_keep": 0}


def test_claim_job_key_is_first_come(fake_redis):
    assert pipeline_progress.claim_job_key(run_id="r1", job_key="k") is True
    assert pipeline_progress.claim_job_key(run_id="r1", job_key="k") is False
    assert pipeline_progress.claim_job_key(run_id="r2", job_key="k") is True


def test_job_found_by_two_keywords_is_scored_once(monkeypatch):
    llm_calls = []
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: [dict(DUPLICATE_JOB)])

    def fake_llm(job, **kw):
        llm_calls.append(job["url"])
        return {**job, "llm_score": 90, "llm_reason": "fit"}

    monkeypatch.setattr(celery_tasks, "_score_one_job_llm", fake_llm)

    run_id = pipeline_progress.create_run(user_id="u1", total_keywords=2)
    for keyword in ("AI Engineer", "LLM Developer"):
        celery_tasks.search_and_score_keyword(run_id, keyword, ["Pune"], ["linkedin"], {}, SCORE_PARAMS)

    assert llm_calls == [DUPLICATE_JOB["url"]]
    assert len(pipeline_progress.get_jobs(run_id=run_id)) == 1
    assert pipeline_progress.get_run_meta(run_id=run_id)["status"] == "completed"


def test_pipeline_apply_step_passes_user_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: [dict(DUPLICATE_JOB)])
    monkeypatch.setattr(celery_tasks.scorer, "score_jobs", lambda db, jobs, **kw: {"ranked_jobs": jobs})

    def fake_open_and_prepare(jobs, **kw):
        seen.update(kw)
        return {"log": []}

    monkeypatch.setattr(celery_tasks.browser_apply, "open_and_prepare_isolated", fake_open_and_prepare)
    apply_params = {"applicant_info": {"first_name": "A", "last_name": "B", "email": "a@b.c"},
                    "user_id": "user-42", "headless": True}

    celery_tasks.pipeline_task({"keywords": ["x"], "locations": ["Pune"], "sources": ["linkedin"]},
                               SCORE_PARAMS, apply_params)
    assert seen["user_id"] == "user-42"


def test_playwright_runs_on_a_thread_without_an_event_loop(monkeypatch):
    """Regression: sync Playwright refuses threads that have an asyncio loop."""
    import asyncio
    import threading
    from app.services import browser_apply
    seen = {}

    def fake(jobs, **kw):
        seen["thread"] = threading.current_thread().name
        try:
            asyncio.get_running_loop()
            seen["loop"] = True
        except RuntimeError:
            seen["loop"] = False
        return {"log": []}

    monkeypatch.setattr(browser_apply, "open_and_prepare", fake)

    async def caller_with_running_loop():
        return browser_apply.open_and_prepare_isolated([], user_id="u")

    assert asyncio.run(caller_with_running_loop()) == {"log": []}
    assert seen == {"thread": "playwright-apply", "loop": False}


def test_isolated_apply_reraises_errors(monkeypatch):
    from app.services import browser_apply

    def boom(jobs, **kw):
        raise ValueError("browser failed")

    monkeypatch.setattr(browser_apply, "open_and_prepare", boom)
    try:
        browser_apply.open_and_prepare_isolated([])
    except ValueError as e:
        assert "browser failed" in str(e)
    else:
        raise AssertionError("error must propagate")
