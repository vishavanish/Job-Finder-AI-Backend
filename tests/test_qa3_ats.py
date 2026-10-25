"""QA3: company career pages inside Job Search — a wider window for index jobs (§B), a live top-up
during the search (§C), real board names (§D) and the company resolver (§A)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import celery_tasks
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ats import AtsCompany, AtsJob, AtsPlatformSetting
from app.providers.ats import platforms
from app.providers.ats.platforms import AtsRef
from app.providers.job_sources import base
from app.services import ats_crawler, ats_registry, ats_search, pipeline_progress


@pytest.fixture(autouse=True)
def clean_ats():
    db = SessionLocal()
    try:
        db.query(AtsJob).delete()
        db.query(AtsCompany).delete()
        db.query(AtsPlatformSetting).update({AtsPlatformSetting.enabled: True})
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def seed(db, platform: str, slug: str, title: str, *, days_old: float = 0, location: str = "Bengaluru, India",
         n: int = 1) -> AtsCompany:
    """`n` jobs with this title at one company, first seen `days_old` days ago (ashby: no detail call)."""
    row, _ = ats_registry.get_or_create(db, AtsRef(platform, slug), origin="admin", name=slug.title())
    jobs = [{"external_id": f"{slug}-{title}-{i}", "title": title, "location": location, "department": "",
             "url": f"https://jobs.example/{slug}/{title}/{i}", "posted": "", "remote": False, "country": "",
             "description": "Build things"} for i in range(n)]
    ats_crawler.upsert_jobs(db, row, jobs, complete=False, close=False)
    if days_old:
        db.query(AtsJob).filter(AtsJob.company_id == row.id).update(
            {AtsJob.first_seen_at: _now() - timedelta(days=days_old)})
    db.commit()
    return row


def lookup(db, keyword: str, hours: int = 24) -> list[dict]:
    return ats_search.index_jobs(db, keyword, locations=["Bangalore"], country="India", max_age_hours=hours,
                                 limit=25, detail_budget=0)


# ---------------------------------------------------------------- §B window

def test_a_one_day_search_still_finds_last_weeks_career_page_jobs(db):
    seed(db, "ashby", "acme", "Python Developer", days_old=5)
    seed(db, "ashby", "globex", "Python Developer", days_old=20)
    assert [j["company"] for j in lookup(db, "python developer")] == ["Acme"]
    assert [j["company"] for j in lookup(db, "python developer", hours=24 * 30)] == ["Acme", "Globex"]


def test_window_follows_the_setting(db, monkeypatch):
    seed(db, "ashby", "acme", "Python Developer", days_old=5)
    monkeypatch.setattr(get_settings(), "ATS_SEARCH_MIN_DAYS", 1)
    assert lookup(db, "python developer") == []
    assert ats_search.search_window_days(24 * 3) == 3


def test_companies_found_by_job_search_become_hot(db):
    company = seed(db, "ashby", "acme", "Python Developer")
    assert company.hot_until is None
    lookup(db, "python developer")
    db.refresh(company)
    assert company.hot_until is not None and company.hot_until > _now()


# ---------------------------------------------------------------- §C live top-up in the pipeline

PARAMS = {"user_id": None, "resume_summary": "Python dev", "skills": ["python"], "career_targets": [],
          "keyword_prefilter_min_pct": 0, "llm_min_score_to_keep": 70}
SEARCH = {"country_indeed": "India", "max_age_hours": 24, "results_per_keyword": 25}


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr(celery_tasks, "_make_progress_cb", lambda task, name: (lambda msg: None))
    monkeypatch.setattr(celery_tasks.job_sources, "scrape_all", lambda **kw: [])
    monkeypatch.setattr(celery_tasks, "_score_batch_llm",
                        lambda jobs, **kw: [{**j, "llm_score": 90, "llm_reason": "fit"} for j in jobs])
    monkeypatch.setattr(ats_search, "full_description", lambda db, job, company=None: job.snippet)
    calls: list[str] = []

    def fake_topup(keyword: str) -> dict:
        calls.append(keyword)
        s = SessionLocal()
        try:
            seed(s, "ashby", "initech", "Python Developer")
        finally:
            s.close()
        return {"searched": 1, "crawled": 0, "added": 1, "errors": 0}

    monkeypatch.setattr(celery_tasks, "_run_ats_topup", fake_topup)
    return calls


def run(keyword: str = "Python Developer") -> tuple[str, dict]:
    run_id = pipeline_progress.create_run(user_id="u-qa3", total_keywords=1)
    result = celery_tasks.search_and_score_keyword(run_id, keyword, ["Bangalore"], ["company_ats"], SEARCH, PARAMS)
    return run_id, result


def test_thin_index_checks_career_pages_live_and_adds_the_jobs_to_the_run(pipeline):
    run_id, result = run()
    assert pipeline == ["Python Developer"]
    assert result["kept"] == 1
    feed = pipeline_progress.current_jobs(run_id=run_id)
    assert [(j["company"], j["source"]) for j in feed] == [("Initech", "Ashby")]


def test_one_live_check_per_query_per_ten_minutes(pipeline):
    run()
    run()
    assert pipeline == ["Python Developer"]


def test_enough_stored_jobs_means_no_live_check(pipeline, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "ATS_MIN_COMPANIES", 2)
    seed(db, "ashby", "acme", "Python Developer", n=2)
    run()
    assert pipeline == []


def test_no_live_check_when_company_pages_are_not_a_source(pipeline):
    run_id = pipeline_progress.create_run(user_id="u-qa3", total_keywords=1)
    celery_tasks.search_and_score_keyword(run_id, "Python Developer", ["Bangalore"], ["indeed"], SEARCH, PARAMS)
    assert pipeline == []


# ---------------------------------------------------------------- §D real board names

FIXTURES = Path(__file__).parent / "fixtures"


class Resp:
    def __init__(self, body, status_code: int = 200, headers: dict | None = None):
        self.status_code, self.headers = status_code, headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)
        self._body = body

    def json(self):
        return json.loads(self._body) if isinstance(self._body, str) else self._body


@pytest.fixture
def web(monkeypatch):
    """URL substring -> response; records every URL asked for."""
    routes: dict[str, Resp] = {}
    calls: list[str] = []

    def fake(method, url, **kwargs):
        calls.append(url)
        for part in sorted(routes, key=len, reverse=True):
            if part in url:
                return routes[part]
        return Resp({"error": "not routed"}, 404)

    monkeypatch.setattr(base, "request", fake)
    return routes, calls


@pytest.mark.parametrize("title,name", [
    ("Spotify", "Spotify"), ("Ramp Jobs", "Ramp"), ("CAREERS AT NVIDIA", "NVIDIA"),
    ("ThoughtWorks - Current Openings", "ThoughtWorks"), ("Jobs at Razorpay", "Razorpay"),
    ("Careers | Acme", "Acme"), ("Ben &amp; Jerry&#39;s Careers", "Ben & Jerry's"), ("Current Openings", None),
])
def test_page_titles_become_company_names(title, name):
    assert platforms.clean_board_name(title) == name


def test_slug_named_company_gets_its_board_name_once(web, db):
    routes, calls = web
    routes["api.ashbyhq.com/posting-api/job-board/acmeco"] = Resp(
        json.loads((FIXTURES / "sources" / "ashby.json").read_text(encoding="utf-8")))
    routes["jobs.ashbyhq.com/acmeco"] = Resp("<html><head><title>Acme Corp Jobs</title></head></html>")
    row, _ = ats_registry.get_or_create(db, AtsRef("ashby", "acmeco"), origin="seed")
    assert row.name == "Acmeco"
    ats_crawler.crawl_company(db, row.id)
    db.refresh(row)
    assert row.name == "Acme Corp"
    page_calls = [u for u in calls if "jobs.ashbyhq.com" in u]
    assert len(page_calls) == 1
    ats_crawler.crawl_company(db, row.id)
    assert len([u for u in calls if "jobs.ashbyhq.com" in u]) == 1  # named now: no second page request


def test_admin_names_are_kept(web, db):
    routes, calls = web
    routes["api.ashbyhq.com/posting-api/job-board/acmeco"] = Resp(
        json.loads((FIXTURES / "sources" / "ashby.json").read_text(encoding="utf-8")))
    routes["jobs.ashbyhq.com/acmeco"] = Resp("<title>Acme Corp Jobs</title>")
    row, _ = ats_registry.get_or_create(db, AtsRef("ashby", "acmeco"), origin="admin", name="Acmeco")
    ats_crawler.crawl_company(db, row.id)
    db.refresh(row)
    assert row.name == "Acmeco" and not [u for u in calls if "jobs.ashbyhq.com" in u]


def test_a_304_for_a_slug_named_company_asks_for_the_full_list_next_time(web, db):
    routes, _ = web
    routes["boards-api.greenhouse.io/v1/boards/wk/jobs"] = Resp("", 304)
    row, _ = ats_registry.get_or_create(db, AtsRef("greenhouse", "wk"), origin="seed")
    row.etag = 'W/"v1"'
    db.commit()
    ats_crawler.crawl_company(db, row.id)
    db.refresh(row)
    assert row.etag is None and row.name == "Wk"


def test_rippling_name_comes_from_a_job_detail(web):
    routes, _ = web
    routes["ats.rippling.com/api/v2/board/acme/jobs/j1"] = Resp({"companyName": "Acme Robotics"})
    impl = platforms.get("rippling")
    assert impl.board_name(AtsRef("rippling", "acme"), [{"external_id": "j1"}]) == "Acme Robotics"
    assert impl.board_name(AtsRef("rippling", "acme"), []) is None
