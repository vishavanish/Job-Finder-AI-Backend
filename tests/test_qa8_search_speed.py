"""QA8: jobs show after each place, a search queues a few titles at a time, live career-page checks get a slot."""
from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from app.core import celery_tasks
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ats import AtsJob
from app.providers.ats.platforms import AtsRef
from app.services import ats_crawler, ats_registry, job_sources, pipeline_progress

PARAMS = {"user_id": None, "resume_summary": "AI engineer", "skills": ["python"], "career_targets": [],
          "keyword_prefilter_min_pct": 0, "llm_min_score_to_keep": 70}
TITLES = [f"Title {i}" for i in range(7)]
SEARCH = {"keywords": TITLES, "locations": ["Pune"], "sources": ["indeed"]}


def _job(place: str, i: int = 0) -> dict:
    return {"source": "Indeed", "title": f"Python Dev {place} {i}", "company": f"Co {place} {i}", "location": place,
            "url": f"https://indeed.example/{place}-{i}", "description": "python " * 20}


def _score_all(jobs, **kw):
    return [{**j, "llm_score": 90, "llm_reason": "r"} for j in jobs]


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks, "_score_batch_llm", _score_all)


# ---------------------------------------------------------------- A: show jobs after each place

def test_each_place_hands_over_its_jobs_before_the_next_starts(monkeypatch):
    monkeypatch.setattr(job_sources, "_run_jobspy", lambda keyword, location, sites, **kw: (location, None))
    monkeypatch.setattr(job_sources, "_normalize", lambda location: [_job(location)])
    monkeypatch.setattr(job_sources.time, "sleep", lambda s: None)
    handed: list[list[str]] = []
    rest = job_sources.scrape_all(["Python"], ["Pune", "Mumbai"], ["indeed"],
                                  on_place=lambda rows: handed.append([j["location"] for j in rows]))
    assert handed == [["Pune"], ["Mumbai"]]
    assert rest == []  # not handed over twice

    # Without on_place (job alerts, /search) everything comes back as before.
    assert sorted(j["location"] for j in job_sources.scrape_all(["Python"], ["Pune", "Mumbai"], ["indeed"])) == [
        "Mumbai", "Pune"]


def test_the_first_place_is_in_the_feed_while_the_second_is_searched(monkeypatch, quiet):
    feed_at_second_place: list[str] = []
    run_id = pipeline_progress.create_run(user_id="u-qa8", total_keywords=1)

    def scrape(on_place=None, **kw):
        on_place([_job("Pune")])
        feed_at_second_place.extend(j["company"] for j in pipeline_progress.current_jobs(run_id=run_id))
        on_place([_job("Mumbai")])
        return [_job("Remote")]

    monkeypatch.setattr(job_sources, "scrape_all", scrape)
    result = celery_tasks.search_and_score_keyword(run_id, "Python", ["Pune", "Mumbai"], ["indeed"], {}, PARAMS)
    assert feed_at_second_place == ["Co Pune 0"]
    assert result["raw_found"] == 3 and result["kept"] == 3
    assert pipeline_progress.get_run_meta(run_id=run_id)["counts"]["kept"] == 3


# ---------------------------------------------------------------- C: titles take turns

def test_a_search_queues_a_few_titles_and_each_finished_one_starts_the_next(monkeypatch, quiet, dispatched):
    monkeypatch.setattr(get_settings(), "SEARCH_TITLES_AT_ONCE", 3)
    monkeypatch.setattr(job_sources, "scrape_all", lambda **kw: [])
    run_id = pipeline_progress.create_run(user_id="u-qa8", total_keywords=7)
    celery_tasks.dispatch_pipeline_group(run_id, SEARCH, PARAMS)
    queued = [c["args"][1] for c in dispatched if c["task"] == "job_finder.search_and_score_keyword"]
    assert queued == TITLES[:3]

    for title in TITLES:  # the worker runs each one; each hands over to the next waiting title
        celery_tasks.search_and_score_keyword(run_id, title, ["Pune"], ["indeed"], SEARCH, PARAMS)
    queued = [c["args"][1] for c in dispatched if c["task"] == "job_finder.search_and_score_keyword"]
    assert queued == TITLES  # all seven, each once, in order
    assert pipeline_progress.get_run_meta(run_id=run_id)["status"] == "completed"


def test_a_failed_title_still_starts_the_next(monkeypatch, quiet, dispatched):
    def broken(**kw):
        raise RuntimeError("scrape failed")

    monkeypatch.setattr(job_sources, "scrape_all", broken)
    run_id = pipeline_progress.create_run(user_id="u-qa8", total_keywords=2)
    pipeline_progress.hold_titles(run_id=run_id, titles=["Next"])
    result = celery_tasks.search_and_score_keyword(run_id, "First", ["Pune"], ["indeed"], SEARCH, PARAMS)
    assert "error" in result
    assert [c["args"][1] for c in dispatched] == ["Next"]


def test_a_stopped_search_clears_its_waiting_titles_so_it_can_finish(monkeypatch, quiet, dispatched):
    run_id = pipeline_progress.create_run(user_id="u-qa8", total_keywords=2)
    pipeline_progress.hold_titles(run_id=run_id, titles=["Next"])
    monkeypatch.setattr(pipeline_progress, "is_cancelled", lambda rid: True)
    celery_tasks.search_and_score_keyword(run_id, "First", ["Pune"], ["indeed"], SEARCH, PARAMS)
    assert [c["args"][1] for c in dispatched] == ["Next"]  # it runs, sees the cancel and counts itself done


def test_second_users_title_is_queued_before_the_first_users_remaining_ones(monkeypatch, quiet, dispatched):
    monkeypatch.setattr(job_sources, "scrape_all", lambda **kw: [])
    first = pipeline_progress.create_run(user_id="u-a", total_keywords=7)
    second = pipeline_progress.create_run(user_id="u-b", total_keywords=1)
    celery_tasks.dispatch_pipeline_group(first, SEARCH, PARAMS)
    celery_tasks.dispatch_pipeline_group(second, {**SEARCH, "keywords": ["B title"]}, PARAMS)
    celery_tasks.search_and_score_keyword(first, TITLES[0], ["Pune"], ["indeed"], SEARCH, PARAMS)
    order = [c["args"][1] for c in dispatched]
    assert order == [TITLES[0], TITLES[1], TITLES[2], "B title", TITLES[3]]


# ---------------------------------------------------------------- D: live career-page checks

def test_background_crawls_leave_the_live_slot_free(fake_redis):
    limit = get_settings().ATS_HOST_CONCURRENCY
    fake_redis.set("ats:host:x.example", limit)
    with pytest.raises(ats_crawler.HostBusy):
        with ats_crawler.host_slot("x.example"):
            pass
    with ats_crawler.host_slot("x.example", live=True):
        assert int(fake_redis.get("ats:host:x.example")) == limit + 1
    assert int(fake_redis.get("ats:host:x.example")) == limit  # given back, also after the refused try


def test_a_live_check_waits_for_a_slot_instead_of_failing(fake_redis, monkeypatch):
    cfg = get_settings()
    full = cfg.ATS_HOST_CONCURRENCY + cfg.ATS_HOST_LIVE_SLOTS
    fake_redis.set("ats:host:y.example", full)
    with pytest.raises(ats_crawler.HostBusy):
        with ats_crawler.host_slot("y.example", live=True):
            pass

    threading.Timer(0.3, lambda: fake_redis.decr("ats:host:y.example")).start()
    started = time.monotonic()
    with ats_crawler.host_slot("y.example", live=True, wait_seconds=5):
        pass
    assert 0.2 < time.monotonic() - started < 3
    assert int(fake_redis.get("ats:host:y.example")) == full - 1

    fake_redis.set("ats:host:y.example", full)
    with pytest.raises(ats_crawler.HostBusy):  # the wait has a limit
        with ats_crawler.host_slot("y.example", live=True, wait_seconds=0.3):
            pass
    assert int(fake_redis.get("ats:host:y.example")) == full


# ---------------------------------------------------------------- two live checks saving the same job

def _ats_job(ext: str, title: str = "Python Engineer") -> dict:
    return {"external_id": ext, "title": title, "location": "Pune, India", "department": "",
            "url": f"https://jobs.example/{ext[:40]}", "posted": "", "remote": False, "country": "",
            "description": "Build things"}


@pytest.fixture
def ats_company():
    db = SessionLocal()
    row, _ = ats_registry.get_or_create(db, AtsRef("lever", f"qa8-{uuid.uuid4().hex[:8]}"), origin="admin", name="QA8")
    db.commit()
    yield db, row
    db.rollback()
    db.query(AtsJob).filter(AtsJob.company_id == row.id).delete()
    db.delete(row)
    db.commit()
    db.close()


def test_a_job_saved_by_another_check_meanwhile_is_updated_not_lost(ats_company, monkeypatch):
    db, company = ats_company
    # The other check's row is already there, but this check's first read doesn't see it (it read just before).
    db.add(AtsJob(company_id=company.id, external_id="same-job", first_seen_at=ats_crawler._now(),
                  last_seen_at=ats_crawler._now(), **ats_crawler._row_fields(company, _ats_job("same-job", "Older"))))
    db.commit()
    real_query = db.query
    reads: list[bool] = []

    def query(*entities):
        if len(entities) == 1 and entities[0] is AtsJob and not reads:
            reads.append(True)
            return SimpleNamespace(filter=lambda *a: SimpleNamespace(all=lambda: []))
        return real_query(*entities)

    new_rows: list = []
    jobs = [_ats_job("same-job"), _ats_job("other-job", "Data Engineer")]
    with monkeypatch.context() as patch:
        patch.setattr(db, "query", query)
        result = ats_crawler.upsert_jobs(db, company, jobs, complete=False, close=False, new_rows=new_rows)
    db.commit()

    rows = {r.external_id: r for r in db.query(AtsJob).filter(AtsJob.company_id == company.id)}
    assert reads and set(rows) == {"same-job", "other-job"}  # the rest of this company's jobs aren't lost
    assert rows["same-job"].title == "Python Engineer"
    assert result["added"] == 1 and [r.external_id for r in new_rows] == ["other-job"]
    assert company.job_count == 2


def test_long_job_ids_match_their_saved_rows(ats_company):
    db, company = ats_company
    long_id = "x" * 200
    assert ats_crawler.upsert_jobs(db, company, [_ats_job(long_id)], complete=True)["added"] == 1
    db.commit()
    again = ats_crawler.upsert_jobs(db, company, [_ats_job(long_id)], complete=True)  # used to collide, then close it
    db.commit()
    assert again == {"added": 0, "changed": 0, "closed": 0}
    assert db.query(AtsJob).filter(AtsJob.company_id == company.id).one().closed_at is None
