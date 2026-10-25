"""QA2 §1: jobs are scored a few per AI call, shown while they are scored,
and taken back out when they score too low."""
from __future__ import annotations

import json

import pytest

from app.core import celery_tasks
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ai import AiModel
from app.providers.llm import adapters
from app.providers.llm.base import LLMError, LLMResult
from app.services import pipeline_progress, scorer


# ---------------------------------------------------------------- scorer.score_batch

class BatchAdapter:
    """Answers a scoring prompt with one entry per job index it is told to."""

    def __init__(self):
        self.calls: list[int] = []  # jobs per call
        self.skip: set[int] = set()  # indexes the reply leaves out
        self.fail = False

    def complete(self, spec, *, api_key, system, user, json_mode, max_tokens, timeout):
        n = user.count("Index: ")  # one per job in the prompt
        self.calls.append(n)
        if self.fail:
            raise LLMError("HTTP 503 from model endpoint")
        entries = [{"index": i, "score": 80 + i, "reason": f"fit {i}"} for i in range(n) if i not in self.skip]
        self.skip = set()  # only the first (batch) reply leaves jobs out
        return LLMResult(text=json.dumps(entries), tokens_in=100, tokens_out=50)


@pytest.fixture
def one_model(monkeypatch):
    fake = BatchAdapter()
    for name in list(adapters.ADAPTERS):
        monkeypatch.setitem(adapters.ADAPTERS, name, fake)
    db = SessionLocal()
    try:
        m = AiModel(provider="openai_compatible", model_id="acme/batch", display_name="Acme batch",
                    base_url="https://api.acme.example/v1", tier="free", priority=1, enabled_global=True,
                    key_scope="acme-batch", tasks=[])
        db.add(m)
        others = db.query(AiModel).filter(AiModel.enabled_global.is_(True)).all()
        for o in others:
            o.enabled_global = False
        m.enabled_global = True
        db.commit()
        yield fake
        for o in others:
            o.enabled_global = True
        db.delete(m)
        db.commit()
    finally:
        db.close()


def _jobs(n: int) -> list[dict]:
    return [{"title": f"Engineer {i}", "company": f"Co {i}", "source": "Indeed", "description": "python"}
            for i in range(n)]


def test_five_jobs_cost_one_ai_call(one_model):
    db = SessionLocal()
    try:
        jobs = scorer.score_batch(db, _jobs(5), user_id=None, resume_summary="Python dev", career_targets=[])
    finally:
        db.close()
    assert one_model.calls == [5]
    assert [j["llm_score"] for j in jobs] == [80, 81, 82, 83, 84]
    assert not any(j.get("llm_failed") for j in jobs)


def test_jobs_the_reply_left_out_are_scored_one_by_one(one_model):
    one_model.skip = {3}
    db = SessionLocal()
    try:
        jobs = scorer.score_batch(db, _jobs(5), user_id=None, resume_summary="x", career_targets=[])
    finally:
        db.close()
    assert one_model.calls == [5, 1]
    assert all("llm_score" in j for j in jobs) and jobs[3]["llm_score"] == 80


def test_no_model_marks_every_job_unscored(one_model):
    one_model.fail = True
    db = SessionLocal()
    try:
        jobs = scorer.score_batch(db, _jobs(3), user_id=None, resume_summary="x", career_targets=[])
    finally:
        db.close()
    assert all(j["llm_failed"] for j in jobs)


# ---------------------------------------------------------------- pipeline: early feed, batches

PARAMS = {"user_id": None, "resume_summary": "AI engineer", "skills": ["python"], "career_targets": [],
          "keyword_prefilter_min_pct": 0, "llm_min_score_to_keep": 70}


def _scraped(n: int) -> list[dict]:
    return [{"source": "Indeed", "title": f"Python Dev {i}", "company": f"Batch Co {i}", "location": "Pune",
             "url": f"https://indeed.example/batch-{i}", "description": "python " * 20} for i in range(n)]


def test_search_scores_in_batches_and_drops_low_scores(monkeypatch):
    sizes: list[int] = []
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: _scraped(7))

    def fake_batch(jobs, **kw):
        sizes.append(len(jobs))
        # Even companies fit, odd ones don't.
        return [{**j, "llm_score": 90 if int(j["company"].split()[-1]) % 2 == 0 else 40, "llm_reason": "r"}
                for j in jobs]

    monkeypatch.setattr(celery_tasks, "_score_batch_llm", fake_batch)
    monkeypatch.setattr(get_settings(), "SCORE_BATCH_SIZE", 5)
    run_id = pipeline_progress.create_run(user_id="u-batch", total_keywords=1)
    result = celery_tasks.search_and_score_keyword(run_id, "Python", ["Pune"], ["indeed"], {}, PARAMS)

    assert sizes == [5, 2]
    assert result["kept"] == 4 and result["filtered"] == 3
    raw = pipeline_progress.get_jobs(run_id=run_id)
    early = [e for e in raw if e.get("scoring")]
    assert len(early) == 7 and all(e["llm_score"] is None for e in early)  # all shown before scoring
    assert sum(1 for e in raw if e.get("removed")) == 3
    feed = pipeline_progress.current_jobs(run_id=run_id)
    assert sorted(e["company"] for e in feed) == ["Batch Co 0", "Batch Co 2", "Batch Co 4", "Batch Co 6"]
    assert all(e["scoring"] is False and e["llm_score"] == 90 for e in feed)


def test_unscored_jobs_leave_the_feed_and_alerts_never_show_early(monkeypatch):
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: _scraped(2))
    monkeypatch.setattr(celery_tasks, "_score_batch_llm", lambda jobs, **kw: [
        {**j, "llm_failed": True, "llm_score": 0} for j in jobs])
    run_id = pipeline_progress.create_run(user_id="u-unscored", total_keywords=1)
    result = celery_tasks.search_and_score_keyword(run_id, "Python", ["Pune"], ["indeed"], {}, PARAMS)
    assert result["unscored"] == 2 and pipeline_progress.current_jobs(run_id=run_id) == []

    db = SessionLocal()
    try:
        alert_run = pipeline_progress.create_run(user_id="u-alert", total_keywords=1, kind="alert")
        monkeypatch.setattr(celery_tasks, "_score_batch_llm", lambda jobs, **kw: [
            {**j, "llm_score": 95, "llm_reason": "r"} for j in jobs])
        counts, kept = celery_tasks._process_jobs(db, alert_run, _scraped(2), score_params=PARAMS,
                                                  request_keys={}, show_early=False)
    finally:
        db.close()
    assert counts["kept"] == 2 and len(kept) == 2
    assert not any(e.get("scoring") for e in pipeline_progress.get_jobs(run_id=alert_run))


def test_cancel_between_batches_takes_waiting_jobs_out(monkeypatch):
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: _scraped(6))
    monkeypatch.setattr(get_settings(), "SCORE_BATCH_SIZE", 3)
    run_id = pipeline_progress.create_run(user_id="u-cancel", total_keywords=1)

    def cancel_after_first(jobs, **kw):
        pipeline_progress.mark_run_status(run_id=run_id, status="cancelled", error="cancelled by you")
        return [{**j, "llm_score": 90, "llm_reason": "r"} for j in jobs]

    monkeypatch.setattr(celery_tasks, "_score_batch_llm", cancel_after_first)
    celery_tasks.search_and_score_keyword(run_id, "Python", ["Pune"], ["indeed"], {}, PARAMS)
    feed = pipeline_progress.current_jobs(run_id=run_id)
    assert len(feed) == 3 and all(not e["scoring"] for e in feed)


# ---------------------------------------------------------------- §1.3 places, §1.4 timings

@pytest.mark.parametrize("locations, keep, skipped", [
    (["India", "Mumbai"], ["India"], ["Mumbai"]),
    (["Mumbai", "Pune"], ["Mumbai", "Pune"], []),
    (["India", "Remote"], ["India", "Remote"], []),
    (["India", "Berlin, Germany"], ["India", "Berlin, Germany"], []),
    (["Germany", "Berlin, Germany", "Pune"], ["Germany", "Pune"], ["Berlin, Germany"]),
    (["india", "Bengaluru, Karnataka, India"], ["india"], ["Bengaluru, Karnataka, India"]),
])
def test_search_locations_skip_places_a_wider_one_covers(locations, keep, skipped):
    from app.services.job_sources import search_locations
    assert search_locations(locations, "India") == (keep, skipped)


def test_scrape_all_searches_india_once_and_times_the_sources(monkeypatch):
    from app.services import job_sources
    searched: list[str] = []

    def fake_jobspy(keyword, location, sites, **kw):
        searched.append(location)
        return None, None

    monkeypatch.setattr(job_sources, "_run_jobspy", fake_jobspy)
    monkeypatch.setattr(job_sources, "_normalize", lambda df: [])
    monkeypatch.setattr(job_sources.time, "sleep", lambda s: None)
    timings: dict[str, float] = {}
    job_sources.scrape_all(["AI Engineer"], ["India", "Mumbai"], ["linkedin", "indeed"], timings=timings)
    assert searched == ["India"]
    assert set(timings) == {"LinkedIn + Indeed"}


def test_run_meta_sums_timings_over_keyword_tasks():
    run_id = pipeline_progress.create_run(user_id="u-time", total_keywords=2)
    pipeline_progress.add_timings(run_id=run_id, timings={"LinkedIn + Indeed": 30.04, "Scoring": 5})
    pipeline_progress.add_timings(run_id=run_id, timings={"LinkedIn + Indeed": 10, "Scoring": 0})
    assert pipeline_progress.get_run_meta(run_id=run_id)["timings"] == {"LinkedIn + Indeed": 40.0, "Scoring": 5.0}
